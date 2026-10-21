/* WebOps AI Agent dashboard — all data comes from the API (and therefore the database). */
"use strict";

// ---------- tiny DOM helpers (textContent only: scraped text is never parsed as HTML) ----------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
// Element.append(null) would insert the text "null"; put() skips empty children.
const put = (el, ...kids) => { for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) el.append(k); return el; };

function storage(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; }
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const key = storage("webops.apiKey");
  if (key) headers["X-API-Key"] = key;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg += `: ${typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)}`; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 4500);
}

// ---------- formatting ----------
const fmtNum = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtMoney = (v, c) => (v === null || v === undefined) ? "—" : `${fmtNum(v)}${c ? " " + c : ""}`;
function fmtPct(v) {
  if (v === null || v === undefined) return h("span", { class: "muted" }, "—");
  const cls = v > 0 ? "up" : v < 0 ? "down" : "";
  return h("span", { class: cls }, `${v > 0 ? "▲ +" : v < 0 ? "▼ " : ""}${fmtNum(v)}%`);
}
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// Status always = icon + label + color (never color alone).
const QUALITY = {
  VERIFIED: ["good", "✓", "Verified"], LIKELY_VALID: ["good", "✓", "Likely valid"],
  NEEDS_REVIEW: ["warning", "!", "Needs review"], FAILED: ["critical", "✗", "Failed"],
};
const RUN = {
  completed: ["good", "✓", "Completed"], completed_with_errors: ["serious", "!", "Completed with errors"],
  failed: ["critical", "✗", "Failed"], running: ["neutral", "●", "Running"], queued: ["neutral", "…", "Queued"],
};
const SEVERITY = { high: ["critical", "▲", "High"], medium: ["serious", "●", "Medium"], low: ["warning", "●", "Low"] };
function badge(map, key) {
  const [cls, , label] = map[key] || ["neutral", "", key || "—"];
  return h("span", { class: `badge ${cls}` }, h("span", { class: "dot" }), label);
}
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function table(el, columns, rows, onRow) {
  clear(el);
  el.appendChild(h("thead", {}, h("tr", {}, columns.map(c => h("th", { class: c.num ? "num" : "" }, c.label)))));
  const tb = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: onRow ? "clickable" : "" }, columns.map(c => {
      const v = c.render ? c.render(r) : r[c.key];
      return h("td", { class: [c.num ? "num" : "", c.cls || ""].join(" ") }, v === null || v === undefined ? "—" : v);
    }));
    if (onRow) tr.addEventListener("click", () => onRow(r));
    tb.appendChild(tr);
  }
  el.appendChild(tb);
}

// ---------- navigation ----------
const views = ["overview", "prices", "runs", "alerts", "errors", "workflows", "import"];
let current = "overview";
function show(view) {
  current = view;
  for (const v of views) $(`view-${v}`).classList.toggle("hidden", v !== view);
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.view === view);
  storage("webops.view", view);
  loaders[view]?.();
}
document.querySelectorAll("#nav button").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => show(b.dataset.goto)));

// ---------- overview ----------
let runsChart = null;
async function loadOverview() {
  const [o, alerts] = await Promise.all([api("/api/dashboard/overview"), api("/api/alerts?status=open&limit=8")]);
  const tiles = clear($("tiles"));
  const tile = (label, value, sub) => tiles.appendChild(h("div", { class: "tile" },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null));
  tile("Websites monitored", o.websites_monitored);
  tile("Products monitored", o.products_monitored);
  tile("Successful checks", o.successful_checks, `${o.verified_checks} verified`);
  tile("Failed checks", o.failed_checks, `${o.needs_review_checks} need review`);
  tile("Price changes (30d)", o.price_changes_30d, `${o.price_increases_30d} up · ${o.price_decreases_30d} down`);
  tile("Open alerts", o.alerts_open, `${o.alerts_total} total`);
  const last = o.last_execution;
  tile("Last execution", last ? fmtAgo(last.finished_at || last.started_at) : "never",
    last ? `run #${last.id} · ${(RUN[last.status] || [, , last.status])[2]}` : "run a workflow to start");

  const ob = $("onboarding");
  if (o.products_monitored === 0) {
    clear(ob).append(h("strong", {}, "Connect your first real websites"),
      h("ol", {},
        h("li", {}, "Copy config/websites.example.yaml to config/websites.yaml and add the websites and product URLs you are authorized to monitor — or import a CSV in Data Onboarding."),
        h("li", {}, "Inspect a product page first: python -m app.cli inspect <product-url>"),
        h("li", {}, "Create a workflow from a plain-language instruction in Workflows, then press Run now.")));
    ob.classList.remove("hidden");
  } else if (o.selector_repairs_pending) {
    put(clear(ob), h("strong", {}, `${o.selector_repairs_pending} selector repair(s) awaiting approval. `),
      "A monitored website changed its page structure. ",
      h("button", { class: "link", onclick: () => show("errors") }, "Review repairs"));
    ob.classList.remove("hidden");
  } else ob.classList.add("hidden");

  // Recent runs: stacked bars by data quality (status palette; legend + table-equivalent in Execution Monitor).
  const runs = o.recent_runs;
  $("runs-chart-empty").classList.toggle("hidden", runs.length > 0);
  $("runs-chart").parentElement.classList.toggle("hidden", runs.length === 0);
  if (runs.length && window.Chart) {
    const surface = css("--surface-1");
    const mk = (label, key, color) => ({ label, data: runs.map(r => r[key]), backgroundColor: color, borderColor: surface,
      borderWidth: { top: 2, left: 0, right: 0, bottom: 0 }, borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 28 });
    const data = { labels: runs.map(r => `#${r.id}`), datasets: [
      mk("Succeeded", "succeeded", css("--good")), mk("Needs review", "needs_review", css("--warning")), mk("Failed", "failed", css("--critical"))] };
    const opts = chartBase();
    opts.scales.x.stacked = true; opts.scales.y.stacked = true; opts.scales.y.ticks.precision = 0;
    opts.plugins.legend = { display: true, position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, color: css("--text-secondary") } };
    opts.interaction = { mode: "index", intersect: false };
    opts.onClick = (_, els) => { if (els.length) { selectedRun = runs[els[0].index].id; show("runs"); } };
    if (runsChart) runsChart.destroy();
    runsChart = new Chart($("runs-chart"), { type: "bar", data, options: opts });
  }

  const box = clear($("overview-alerts"));
  if (!alerts.length) box.appendChild(h("div", { class: "empty" }, "No open alerts."));
  for (const a of alerts) {
    box.appendChild(h("div", { style: "padding:8px 0;border-bottom:1px solid var(--grid)" },
      h("div", { class: "row", style: "margin:0;justify-content:space-between" }, badge(SEVERITY, a.severity),
        h("span", { class: "small muted" }, fmtAgo(a.created_at))),
      h("div", { style: "margin-top:4px" }, a.reason)));
  }
  updateAlertPill(o.alerts_open);
  updateRepairPill(o.selector_repairs_pending);
}
function updateAlertPill(n) { const p = $("nav-alerts"); p.textContent = n; p.classList.toggle("hidden", !n); }

function chartBase() {
  const grid = css("--grid"), muted = css("--text-muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }, tooltip: { backgroundColor: css("--text-primary"), titleColor: css("--surface-1"),
      bodyColor: css("--surface-1"), padding: 10, boxWidth: 10, boxHeight: 2 } },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 18 } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: muted } },
    },
  };
}

// ---------- price intelligence ----------
let pdChart = null;
async function loadPrices() {
  const products = await api("/api/products");
  const empty = $("prices-empty");
  if (!products.length) { clear($("prices-table")); empty.textContent = "No products configured yet. Add real products in Data Onboarding."; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  table($("prices-table"), [
    { label: "Website", key: "website" },
    { label: "Product", render: r => h("span", { class: "ellipsis", title: r.name }, r.name) },
    { label: "Current", num: true, render: r => fmtMoney(r.stats.current_price, r.stats.currency) },
    { label: "Previous", num: true, render: r => fmtMoney(r.stats.previous_price, r.stats.currency) },
    { label: "Change", num: true, render: r => fmtPct(r.stats.percentage_change) },
    { label: "7d", num: true, render: r => fmtPct(r.stats.change_7d_pct) },
    { label: "30d", num: true, render: r => fmtPct(r.stats.change_30d_pct) },
    { label: "Lowest", num: true, render: r => fmtNum(r.stats.lowest_price) },
    { label: "Highest", num: true, render: r => fmtNum(r.stats.highest_price) },
    { label: "Trend", render: r => ({ up: "↗ up", down: "↘ down", flat: "→ flat" })[r.stats.trend] || "—" },
    { label: "Availability", render: r => (r.stats.availability || "—").replace(/_/g, " ") },
    { label: "Last check", render: r => r.latest_status ? badge(QUALITY, r.latest_status) : "never checked" },
  ], products, r => openProduct(r.id));
}

async function openProduct(id) {
  const d = await api(`/api/products/${id}/history`);
  const p = d.product, s = d.stats;
  $("product-detail").classList.remove("hidden");
  $("pd-title").textContent = p.name;
  put(clear($("pd-sub")),`${p.website} · `, h("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, p.url),
    p.sku ? ` · SKU ${p.sku}` : "");
  const st = clear($("pd-stats"));
  const t = (l, v) => st.appendChild(h("div", { class: "tile" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v)));
  t("Current", fmtMoney(s.current_price, s.currency)); t("Previous", fmtMoney(s.previous_price, s.currency));
  t("Change", fmtPct(s.percentage_change)); t("Lowest", fmtNum(s.lowest_price)); t("Highest", fmtNum(s.highest_price));
  t("Average", fmtNum(s.average_price)); t("Trusted observations", s.observations);

  const pts = d.series.filter(x => x.price !== null);
  if (pdChart) pdChart.destroy();
  if (window.Chart && pts.length) {
    const opts = chartBase();
    opts.interaction = { mode: "index", intersect: false };
    opts.plugins.tooltip.callbacks = { label: c => ` ${fmtMoney(c.parsed.y, pts[c.dataIndex].currency)}` };
    opts.scales.y.grace = "10%";
    pdChart = new Chart($("pd-chart"), {
      type: "line",
      data: { labels: pts.map(x => fmtTime(x.t)), datasets: [{ label: "Price", data: pts.map(x => x.price),
        borderColor: css("--series-1"), backgroundColor: css("--series-1"), borderWidth: 2, pointRadius: pts.length > 40 ? 0 : 4,
        pointHoverRadius: 5, pointBorderColor: css("--surface-1"), pointBorderWidth: 2, tension: 0, stepped: false }] },
      options: opts,
      plugins: [crosshair],
    });
  }
  table($("pd-table"), [
    { label: "Observed", render: r => fmtTime(r.observed_at) },
    { label: "Price", num: true, render: r => fmtMoney(r.price, r.currency) },
    { label: "Availability", render: r => (r.availability || "").replace(/_/g, " ") },
    { label: "Data quality", render: r => badge(QUALITY, r.extraction_status) },
    { label: "Method", key: "extraction_method" },
    { label: "Run", render: r => `#${r.run_id}` },
    { label: "Issues", render: r => (r.issues || []).map(i => i.code).join(", ") || "—" },
  ], d.observations);
  $("product-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("pd-close").addEventListener("click", () => $("product-detail").classList.add("hidden"));

const crosshair = {
  id: "crosshair",
  afterDraw(chart) {
    const a = chart.tooltip?.getActiveElements?.();
    if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};

// ---------- execution monitor ----------
let selectedRun = null, runPoll = null;
const STAGES = [["open_website", "Website opened"], ["locate_product", "Product located"], ["extract", "Data extracted"],
  ["validate", "Data validated"], ["store", "Observation stored"], ["compare", "Comparison"], ["alert", "Alerts"]];
const MARK = { ok: "✓", failed: "✗", warning: "⚠", skipped: "–" };

async function loadRuns() {
  const runs = await api("/api/runs?limit=50");
  const list = clear($("run-list"));
  if (!runs.length) { list.appendChild(h("div", { class: "empty" }, "No runs yet. Start one from Workflows.")); return; }
  if (!selectedRun) selectedRun = runs[0].id;
  for (const r of runs) {
    list.appendChild(h("div", { class: `run-item ${r.id === selectedRun ? "active" : ""}`, onclick: () => { selectedRun = r.id; loadRuns(); } },
      h("div", { class: "t" }, h("span", {}, `Run #${r.id}`), badge(RUN, r.status)),
      h("div", { class: "small muted" }, r.workflow_name || ""),
      h("div", { class: "small muted" }, `${fmtTime(r.started_at || r.created_at)} · ${r.products_succeeded}✓ ${r.products_needs_review}! ${r.products_failed}✗`)));
  }
  await loadRun(selectedRun);
}

async function loadRun(id) {
  clearTimeout(runPoll);
  const [run, prods, logs] = await Promise.all([api(`/api/runs/${id}`), api(`/api/runs/${id}/products`), api(`/api/runs/${id}/logs`)]);
  const head = clear($("run-head"));
  const done = !["queued", "running"].includes(run.status);
  put(head,
    h("div", { class: "card-head" }, h("h2", {}, `Run #${run.id} — ${run.workflow_name || ""}`),
      h("div", { class: "row", style: "margin:0" }, badge(RUN, run.status),
        done ? h("a", { href: `/api/runs/${run.id}/export` }, h("button", { class: "primary sm" }, "Download Excel report")) : null)),
    h("div", { class: "run-meta" },
      ...[["Trigger", run.trigger], ["Started", fmtTime(run.started_at)], ["Duration", run.summary?.duration_seconds != null ? `${run.summary.duration_seconds}s` : "—"],
        ["Products", `${prods.length} / ${run.products_total}`], ["Succeeded", run.products_succeeded], ["Needs review", run.products_needs_review],
        ["Failed", run.products_failed], ["Alerts", run.alerts_count]]
        .map(([k, v]) => h("div", {}, h("span", { class: "k" }, k), h("span", { class: "v" }, v ?? "—")))),
    run.error ? h("p", { class: "warn-list small" }, run.error) : null);

  const box = clear($("run-products"));
  if (!prods.length) box.appendChild(h("div", { class: "empty" }, done ? "No product steps recorded." : "Waiting for the browser…"));
  else {
    const grid = h("div", { class: "pipeline" }, h("div", { class: "h" }, "Product"),
      STAGES.map(([, l]) => h("div", { class: "h", style: "text-align:center" }, l)), h("div", { class: "h" }, "Result"));
    for (const p of prods) {
      const detailId = `pd-${p.product_id}`;
      const toggle = () => { const d = document.getElementById(detailId); d.classList.toggle("hidden"); };
      grid.append(h("div", { style: "cursor:pointer", onclick: toggle },
        h("div", { class: "prod-name" }, p.product || `#${p.product_id}`), h("div", { class: "small muted" }, p.website || "",
          p.attempts > 1 ? ` · ${p.attempts} attempts` : "")));
      for (const [k] of STAGES) {
        const s = p.stages[k];
        grid.append(h("div", { class: `st ${s || "none"}`, title: p.messages[k] || "" }, s ? MARK[s] || "·" : "·"));
      }
      const o = p.observation;
      grid.append(h("div", {}, o ? badge(QUALITY, o.extraction_status) : h("span", { class: "muted" }, "…"),
        o && o.price != null ? h("div", { class: "small" }, fmtMoney(o.price, o.currency)) : null));
      const extra = [["recovery", "Recovery"], ["selector_repair", "Selector repair"]];
      const msgs = h("ul", {}, [...STAGES, ...extra].filter(([k]) => p.messages[k]).map(([k, l]) => h("li", {}, h("strong", {}, `${l}: `), p.messages[k])));
      const issues = (o?.issues || []).length ? h("ul", {}, o.issues.map(i => h("li", {}, `${i.severity.toUpperCase()} ${i.code}: ${i.message}`))) : null;
      const thumbs = h("div", { class: "thumbs" }, p.screenshots.map(s => h("div", { class: "thumb", onclick: () => lightbox(s.id, `${p.product} — ${s.kind}`) },
        h("img", { src: `/api/screenshots/${s.id}`, loading: "lazy", alt: s.kind }), h("div", {}, s.kind.replace(/_/g, " ")))));
      grid.append(h("div", { class: "prod-detail hidden", id: detailId }, msgs, issues ? h("strong", { class: "small" }, "Validation issues") : null, issues,
        h("div", { class: "small muted", style: "margin-bottom:6px" }, h("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, p.url)), thumbs));
    }
    box.append(h("div", { class: "table-wrap" }, grid));
  }
  const lg = clear($("run-logs"));
  for (const l of logs) lg.append(h("div", { class: l.level }, `${new Date(l.created_at).toLocaleTimeString()}  ${l.level.padEnd(7)} ${l.message}`));
  if (!done && current === "runs") runPoll = setTimeout(() => loadRuns(), 2000);
}

function lightbox(id, cap) {
  $("lightbox-img").src = `/api/screenshots/${id}`; $("lightbox-cap").textContent = cap; $("lightbox").classList.remove("hidden");
}
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));
document.addEventListener("keydown", e => { if (e.key === "Escape") $("lightbox").classList.add("hidden"); });

// ---------- alerts ----------
let alertFilter = "open";
document.querySelectorAll("#alert-filter button").forEach(b => b.addEventListener("click", () => {
  alertFilter = b.dataset.s; document.querySelectorAll("#alert-filter button").forEach(x => x.classList.toggle("active", x === b)); loadAlerts();
}));
async function loadAlerts() {
  const rows = await api(`/api/alerts${alertFilter ? `?status=${alertFilter}` : ""}`);
  $("alerts-empty").classList.toggle("hidden", rows.length > 0);
  const act = (a, status, label) => h("button", { class: "sm", onclick: async (e) => {
    e.stopPropagation();
    try { await api(`/api/alerts/${a.id}`, { method: "PATCH", json: { status } }); loadAlerts(); } catch (err) { toast(`Failed: ${err.message}`); }
  } }, label);
  table($("alerts-table"), [
    { label: "When", render: r => fmtTime(r.created_at) },
    { label: "Severity", render: r => badge(SEVERITY, r.severity) },
    { label: "Type", render: r => r.alert_type.replace(/_/g, " ") },
    { label: "Website", key: "website" },
    { label: "Reason", render: r => h("span", { class: "ellipsis", title: r.reason }, r.reason) },
    { label: "Previous", num: true, key: "previous_value" }, { label: "Current", num: true, key: "current_value" },
    { label: "Change", num: true, render: r => fmtPct(r.percentage_change) },
    { label: "Run", render: r => r.run_id ? h("button", { class: "link", onclick: () => { selectedRun = r.run_id; show("runs"); } }, `#${r.run_id}`) : "—" },
    { label: "Status", render: r => h("div", { class: "row", style: "margin:0;gap:6px" }, r.status,
      r.status === "open" ? act(r, "acknowledged", "Ack") : null, r.status !== "resolved" ? act(r, "resolved", "Resolve") : null) },
  ], rows);
}

// ---------- errors & selector repairs ----------
const ANCHOR = {
  json_ld: "the page's JSON-LD product data", microdata: "the page's microdata", meta: "the page's meta tags",
  structured: "the page's structured data", previous_observation: "the last trusted price (weaker evidence: verify it)",
};
async function loadRepairs() {
  const items = await api("/api/selector-suggestions?status=pending");
  updateRepairPill(items.length);
  const box = clear($("repairs"));
  if (!items.length) { box.append(h("div", { class: "empty" }, "No pending repairs — all configured selectors are working.")); return; }
  for (const s of items) {
    const choices = [s.proposed_selector, ...(s.alternatives || [])];
    const pick = h("select", { "aria-label": "Selector to apply" }, choices.map(c => h("option", { value: c }, c)));
    const decide = (action) => async () => {
      try {
        await api(`/api/selector-suggestions/${s.id}/${action}`, action === "approve" ? { method: "POST", json: { selector: pick.value } } : { method: "POST" });
        toast(action === "approve" ? "Repair applied — next run uses the new selector." : "Suggestion rejected."); loadRepairs();
      } catch (e) { toast(`Failed: ${e.message}`); }
    };
    box.append(h("div", { class: "repair" },
      h("div", {},
        h("div", {}, h("strong", {}, s.website), s.scope === "product" ? ` · ${s.product}` : " · all products", ` · ${s.field.replace("_", " ")}`),
        h("div", { style: "margin:6px 0" }, h("span", { class: "sel old" }, s.old_selector || "(none)"), "  →  ", pick),
        h("div", { class: "small muted" }, `Matched “${s.sample_text}” on ${s.product || "the page"}, which equals ${s.anchor_value} from ${ANCHOR[s.anchor_source] || s.anchor_source}.`),
        h("div", { class: "small muted" }, `Confidence ${Math.round(s.confidence * 100)}% · seen ${s.occurrences}× · last ${fmtAgo(s.last_seen_at)}`)),
      h("div", { class: "actions" }, h("button", { class: "primary sm", onclick: decide("approve") }, "Approve"),
        h("button", { class: "sm", onclick: decide("reject") }, "Reject"))));
  }
}
function updateRepairPill(n) { const p = $("nav-repairs"); p.textContent = n; p.classList.toggle("hidden", !n); }

async function loadErrors() {
  loadRepairs();
  const rows = await api("/api/errors");
  $("errors-empty").classList.toggle("hidden", rows.length > 0);
  table($("errors-table"), [
    { label: "When", render: r => fmtTime(r.observed_at) },
    { label: "Status", render: r => badge(QUALITY, r.extraction_status) },
    { label: "Website", key: "website" }, { label: "Product", key: "product" },
    { label: "Issues", render: r => h("div", {}, (r.issues || []).filter(i => i.severity !== "info").map(i => h("div", { class: "small" }, `${i.code}: ${i.message}`))) },
    { label: "Screenshot", render: r => r.screenshot_id ? h("button", { class: "link", onclick: () => lightbox(r.screenshot_id, `${r.product} — evidence`) }, "View") : "—" },
    { label: "Run", render: r => h("button", { class: "link", onclick: () => { selectedRun = r.run_id; show("runs"); } }, `#${r.run_id}`) },
  ], rows);
}

// ---------- workflows ----------
let lastPlanInstruction = null;
$("wf-plan").addEventListener("click", async () => {
  const instruction = $("wf-instruction").value.trim();
  if (!instruction) return toast("Describe the workflow first.");
  $("wf-plan-status").textContent = "Planning…"; $("wf-plan").disabled = true;
  try {
    const plan = await api("/api/workflows/plan", { method: "POST", json: { instruction } });
    lastPlanInstruction = instruction; renderPlan(plan);
    $("wf-plan-status").textContent = plan.planner === "rule-based" ? "Planned with deterministic rules (no LLM key configured)." : `Planned by ${plan.planner}.`;
  } catch (e) { $("wf-plan-status").textContent = `Planning failed: ${e.message}`; }
  finally { $("wf-plan").disabled = false; }
});
function renderPlan(plan) {
  const d = plan.definition, out = clear($("wf-plan-out")); out.classList.remove("hidden");
  out.append(h("div", { class: "plan" },
    h("h2", {}, plan.name), h("p", { class: "muted" }, plan.rationale),
    h("div", { class: "kv small" },
      h("span", {}, h("strong", {}, "Websites: "), d.websites.join(", ") === "*" ? "all configured" : d.websites.join(", ")),
      h("span", {}, h("strong", {}, "Threshold: "), d.price_change_threshold != null ? `${d.price_change_threshold}%` : "default"),
      h("span", {}, h("strong", {}, "Detect: "), [d.detect_increases && "increases", d.detect_decreases && "decreases", d.detect_availability && "availability"].filter(Boolean).join(", ")),
      h("span", {}, h("strong", {}, "Schedule: "), plan.execution_plan[plan.execution_plan.length - 1].replace("Schedule: ", ""))),
    plan.warnings.length ? h("ul", { class: "warn-list small" }, plan.warnings.map(w => h("li", {}, w))) : null,
    h("strong", {}, "Execution plan"), h("ol", { class: "small" }, plan.execution_plan.map(s => h("li", {}, s))),
    h("button", { class: "primary", onclick: async () => {
      try { await api("/api/workflows", { method: "POST", json: { instruction: lastPlanInstruction } }); toast("Workflow saved."); out.classList.add("hidden"); $("wf-instruction").value = ""; loadWorkflows(); }
      catch (e) { toast(`Save failed: ${e.message}`); }
    } }, "Save workflow")));
}
async function loadWorkflows() {
  const rows = await api("/api/workflows");
  $("wf-empty").classList.toggle("hidden", rows.length > 0);
  table($("wf-table"), [
    { label: "Workflow", render: r => h("div", {}, h("strong", {}, r.name), r.instruction ? h("div", { class: "small muted ellipsis", title: r.instruction }, `“${r.instruction}”`) : null) },
    { label: "Websites", render: r => (r.definition.websites || []).join(", ") === "*" ? "all" : r.definition.websites.join(", ") },
    { label: "Threshold", num: true, render: r => r.definition.price_change_threshold != null ? `${r.definition.price_change_threshold}%` : "default" },
    { label: "Schedule", key: "schedule_text" },
    { label: "Next run", render: r => r.enabled && r.next_run_at ? fmtTime(r.next_run_at) : "—" },
    { label: "Last run", render: r => r.last_run ? h("button", { class: "link", onclick: () => { selectedRun = r.last_run.id; show("runs"); } }, `#${r.last_run.id} `, badge(RUN, r.last_run.status)) : "never" },
    { label: "Source", key: "source" },
    { label: "", render: r => h("div", { class: "row", style: "margin:0;gap:6px" },
      h("button", { class: "primary sm", onclick: async () => {
        try { const run = await api(`/api/workflows/${r.id}/run`, { method: "POST" }); selectedRun = run.id; toast(`Run #${run.id} started — opening the Execution Monitor.`); show("runs"); }
        catch (e) { toast(`Could not start: ${e.message}`); }
      } }, "Run now"),
      h("button", { class: "sm", onclick: async () => {
        try { await api(`/api/workflows/${r.id}`, { method: "PATCH", json: { enabled: !r.enabled } }); loadWorkflows(); } catch (e) { toast(e.message); }
      } }, r.enabled ? "Disable" : "Enable")) },
  ], rows);
}

// ---------- import / config ----------
$("imp-go").addEventListener("click", async () => {
  const f = $("imp-file").files[0];
  if (!f) return toast("Choose a file first.");
  const fd = new FormData(); fd.append("file", f);
  const dry = $("imp-dry").checked;
  try {
    const s = await api(`/api/import/products?dry_run=${dry}`, { method: "POST", body: fd });
    const out = clear($("imp-out"));
    put(out, h("div", { class: "tiles compact", style: "margin-top:14px" },
      [["Rows", s.total_rows], ["Valid", s.valid], [dry ? "Would import" : "Imported", dry ? s.valid : s.imported], ["Updated", s.updated], ["Duplicates", s.duplicates], ["Invalid", s.invalid]]
        .map(([l, v]) => h("div", { class: "tile" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v)))));
    if (s.websites_created.length) out.append(h("p", { class: "small" }, `New websites: ${s.websites_created.join(", ")} (allowlist = product URL domain)`));
    if (s.errors.length) {
      const t = h("table");
      table(t, [{ label: "Row", key: "row" }, { label: "URL", render: r => h("span", { class: "ellipsis" }, r.product_url || "—") }, { label: "Problem", render: r => r.errors.join("; ") }], s.errors);
      out.append(h("div", { class: "table-wrap" }, t));
    }
    if (!dry) loadSites();
  } catch (e) { toast(`Import failed: ${e.message}`); }
});
$("imp-template").addEventListener("click", () => {
  const blob = new Blob(["supplier,product_name,sku,product_url,currency,price_change_threshold,active\n"], { type: "text/csv" });
  const a = h("a", { href: URL.createObjectURL(blob), download: "products_template.csv" }); document.body.append(a); a.click(); a.remove();
});
$("cfg-sync").addEventListener("click", async () => {
  try { const r = await api("/api/config/sync", { method: "POST" }); const o = $("cfg-out"); o.textContent = JSON.stringify(r, null, 2); o.classList.remove("hidden"); loadSites(); }
  catch (e) { toast(`Sync failed: ${e.message}`); }
});
$("api-key").value = storage("webops.apiKey") || "";
$("api-key-save").addEventListener("click", () => {
  const v = $("api-key").value.trim();
  if (v.startsWith("sk-ant-")) { toast("That looks like an Anthropic key. Put it in .env as ANTHROPIC_API_KEY, not here."); return; }
  storage("webops.apiKey", v); toast(v ? "API key saved in this browser." : "API key cleared.");
});
async function loadSites() {
  const rows = await api("/api/websites");
  $("sites-empty").classList.toggle("hidden", rows.length > 0);
  $("sites-table").classList.toggle("hidden", rows.length === 0);
  table($("sites-table"), [{ label: "Website", key: "name" }, { label: "Allowed domains", render: r => r.allowed_domains.join(", ") },
    { label: "Products", num: true, key: "active_products" }, { label: "Selectors", render: r => r.has_selectors ? "configured" : "auto" },
    { label: "Login", render: r => r.requires_login ? "yes" : "no" }, { label: "Source", key: "source" }], rows);
}

// ---------- boot ----------
const loaders = { overview: loadOverview, prices: loadPrices, runs: loadRuns, alerts: loadAlerts, errors: loadErrors, workflows: loadWorkflows, import: loadSites };
async function boot() {
  try {
    const s = await api("/api/system");
    clear($("system-info")).append(
      h("div", {}, `AI planner: ${s.llm_enabled ? s.llm_model : "rule-based (no key)"}`),
      h("div", {}, `Scheduler: ${s.scheduler_enabled ? "on" : "off"}`),
      h("div", {}, `robots.txt: ${s.respect_robots_txt ? "respected" : "ignored"}`),
      h("div", {}, h("a", { href: "/docs", target: "_blank" }, "API docs")));
  } catch { /* server not reachable; the views will show errors */ }
  const v = storage("webops.view");
  show(views.includes(v) ? v : "overview");
  setInterval(() => { if (current === "overview") loadOverview().catch(() => {}); }, 15000);
}
window.addEventListener("error", e => toast(e.message));
window.addEventListener("unhandledrejection", e => toast(e.reason?.message || "Request failed"));
boot();
