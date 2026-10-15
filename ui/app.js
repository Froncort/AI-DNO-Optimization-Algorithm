/*
 * Copyright 2026 Froncort
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* DNO UI — wizard, API client, run orchestration, results */

const L = window.DNO_LINGO;

const TOTAL_STEPS = 5;

// Bundled sample network — matches samples/demo/ in the repo
const DEMO_NETWORK_PATH = "samples/demo/network.json";
const DEMO_CATALOGUE_PATH = "samples/demo/network.catalogue.json";

const state = {
  token: null,
  currentStep: 1,
  setupDone: false,
  networkPath: null,
  cataloguePath: null,
  network: null,
  catalogue: null,
  currentJobId: null,
  currentRunPath: null,
  lastRunMode: null,
  stepComplete: { 1: false, 2: false, 3: false, 4: false, 5: false },
};

let editor = null;
let eventSource = null;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers["X-DNO-Token"] = state.token;
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, { ...options, headers });
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  if (!res.ok) throw new Error(res.statusText);
  return res;
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2800);
}

function appendLog(text) {
  const body = document.getElementById("log-body");
  body.textContent += text + "\n";
  body.scrollTop = body.scrollHeight;
}

function clearLog() {
  document.getElementById("log-body").textContent = "";
}

function isStepComplete(step) {
  return !!state.stepComplete[step];
}

function canAccessStep(step) {
  if (step < 1 || step > TOTAL_STEPS) return false;
  for (let s = 1; s < step; s += 1) {
    if (!isStepComplete(s)) return false;
  }
  return true;
}

function firstIncompleteStep() {
  for (let s = 1; s <= TOTAL_STEPS; s += 1) {
    if (!isStepComplete(s)) return s;
  }
  return TOTAL_STEPS;
}

function stepBlockedMessage(step) {
  const blocker = firstIncompleteStep();
  if (blocker >= step) return L.toast.stepLocked;
  switch (blocker) {
    case 1: return L.toast.completeSetup;
    case 2: return L.toast.chooseNetwork;
    case 3: return L.toast.saveInputsFirst;
    case 4: return L.toast.completeAnalysisFirst;
    default: return L.toast.stepLocked;
  }
}

function resetDownstreamSteps(fromStep) {
  for (let s = fromStep; s <= TOTAL_STEPS; s += 1) {
    state.stepComplete[s] = false;
  }
}

function syncStepCompletion() {
  state.stepComplete[1] = !!state.setupDone;
  state.stepComplete[2] = !!(state.setupDone && state.networkPath && state.network);
  if (!state.stepComplete[2]) resetDownstreamSteps(3);
  else if (!state.stepComplete[3]) resetDownstreamSteps(4);
  else if (!state.stepComplete[4]) resetDownstreamSteps(5);
}

function updateStepNav() {
  syncStepCompletion();

  if (!canAccessStep(state.currentStep)) {
    let target = 1;
    for (let s = 1; s <= TOTAL_STEPS; s += 1) {
      if (canAccessStep(s)) target = s;
    }
    state.currentStep = target;
  }

  document.querySelectorAll(".step-panel").forEach((p) => {
    p.classList.toggle("active", parseInt(p.dataset.step, 10) === state.currentStep);
  });
  document.querySelectorAll(".step-tab").forEach((t) => {
    const s = parseInt(t.dataset.step, 10);
    t.classList.toggle("active", s === state.currentStep);
    t.classList.toggle("done", isStepComplete(s));
    t.disabled = !canAccessStep(s);
    t.setAttribute("aria-disabled", String(!canAccessStep(s)));
  });

  const activeTab = document.querySelector(`.step-tab[data-step="${state.currentStep}"]`);
  if (activeTab && window.matchMedia("(max-width: 768px)").matches) {
    activeTab.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }
  if (state.currentStep === 4) updateCommandPreview();
  if (state.currentStep === 5) refreshRunHistory();
  if (state.currentStep === 3 && editor) updateStep3Continue();
}

function goToStep(n) {
  if (!canAccessStep(n)) {
    toast(stepBlockedMessage(n));
    return false;
  }
  state.currentStep = n;
  updateStepNav();
  return true;
}

function markStepDone(step) {
  if (step >= 1 && step <= TOTAL_STEPS) {
    state.stepComplete[step] = true;
  }
  updateStepNav();
}

async function refreshStatus() {
  const status = await api("/api/status");
  state.token = status.token;
  state.setupDone = status.ready;
  renderEnvCard(status);
  refreshRunHistory(status.runs);
  updateSetupUI(status);
  updateStepNav();
  return status;
}

function updateSetupUI(status) {
  const ready = status.ready;
  document.getElementById("setup-success").classList.toggle("hidden", !ready);
  document.getElementById("setup-panel").classList.toggle("hidden", ready);
  const setupBtn = document.getElementById("btn-setup");
  setupBtn.disabled = false;
  setupBtn.textContent = "Install everything";
}

function renderEnvCard(status) {
  const grid = document.getElementById("env-grid");
  const items = [
    {
      key: "os",
      name: L.setup.items.os.name,
      detail: `${status.os} ${status.os_release}`,
      state: "ok",
      badge: L.setup.items.os.badgeOk,
    },
    {
      key: "python",
      name: L.setup.items.python.name,
      detail: status.python.ok
        ? `Version ${status.python.version} — ready to use`
        : `Version ${status.python.version} — needs 3.10 or newer`,
      state: status.python.ok ? "ok" : "err",
      badge: status.python.ok ? L.setup.items.python.badgeOk : L.setup.items.python.badgeErr,
    },
    {
      key: "venv",
      name: L.setup.items.venv.name,
      detail: status.venv.exists
        ? L.setup.items.venv.detailOk
        : L.setup.items.venv.detailPending,
      state: status.venv.exists ? "ok" : "pending",
      badge: status.venv.exists ? L.setup.items.python.badgeOk : "Pending",
    },
    {
      key: "dno",
      name: L.setup.items.dno.name,
      detail: status.dno_installed
        ? L.setup.items.dno.detailOk
        : L.setup.items.dno.detailPending,
      state: status.dno_installed ? "ok" : "pending",
      badge: status.dno_installed ? L.setup.items.python.badgeOk : "Pending",
    },
    {
      key: "cbc",
      name: L.setup.items.solver.name,
      detail: status.cbc_available
        ? L.setup.items.solver.detailOk
        : L.setup.items.solver.detailPending,
      state: status.cbc_available ? "ok" : "pending",
      badge: status.cbc_available ? L.setup.items.python.badgeOk : "Pending",
    },
  ];

  grid.innerHTML = items.map((item) => {
    const icon = item.state === "ok" ? "✓" : item.state === "err" ? "!" : "○";
    return `<li class="setup-req setup-req--${item.state}">
      <span class="setup-req-icon" aria-hidden="true">${icon}</span>
      <div class="setup-req-body">
        <span class="setup-req-name">${item.name}</span>
        <span class="setup-req-detail">${item.detail}</span>
      </div>
      <span class="setup-req-badge">${item.badge}</span>
    </li>`;
  }).join("");
}

async function loadDemoSample() {
  state.networkPath = DEMO_NETWORK_PATH;
  state.cataloguePath = DEMO_CATALOGUE_PATH;
  const networkInput = document.getElementById("network-path-input");
  const catalogueInput = document.getElementById("catalogue-path-input");
  if (networkInput) networkInput.value = DEMO_NETWORK_PATH;
  if (catalogueInput) catalogueInput.value = DEMO_CATALOGUE_PATH;
  await loadNetwork();
  showSelectedNetwork();
  markStepDone(2);
  toast(L.toast.demoLoaded);
}

async function loadNetwork() {
  if (!state.networkPath) return;
  const qs = new URLSearchParams({ network: state.networkPath });
  if (state.cataloguePath) qs.set("catalogue", state.cataloguePath);
  const data = await api(`/api/network?${qs}`);
  state.network = data.network;
  state.catalogue = data.catalogue || null;
  state.cataloguePath = data.catalogue_path || state.cataloguePath;
  resetDownstreamSteps(3);
  if (editor) {
    editor.load(state.network, state.catalogue, state.networkPath, state.cataloguePath);
  }
  updateStepNav();
}

function showSelectedNetwork() {
  const el = document.getElementById("selected-network");
  const info = document.getElementById("selected-network-info");
  el.classList.remove("hidden");
  const nLabs = (state.network?.labs || []).length;
  const nFac = (state.network?.facilities || []).length;
  info.innerHTML = `
    <p class="selected-network-name">${esc(state.network?.project_id || "Scenario")}</p>
    <p class="selected-network-stats">${L.data.hfLabs(nFac, nLabs)}</p>
    <p class="selected-network-path"><code>${esc(state.networkPath)}</code></p>`;
  setText("step-2-continue-msg", L.data.scenarioReady);
  setText("btn-go-edit", L.data.continueToInputs);
  requestAnimationFrame(() => {
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function updateStep3Continue(findings) {
  const bar = document.getElementById("step-3-continue");
  if (!bar || !editor) return;
  const results = findings || editor.validate();
  const hasErrors = results.some((f) => f.level === "error");
  const hasWarnings = results.some((f) => f.level === "warning");
  bar.classList.toggle("hidden", hasErrors);

  const msg = document.getElementById("step-3-continue-msg");
  const btn = document.getElementById("btn-go-run");
  if (msg) {
    msg.textContent = hasWarnings ? L.editor.validationWarnContinue : L.editor.validationOk;
  }
  if (btn) {
    btn.textContent = isStepComplete(3) ? L.data.continueToAnalysis : L.data.saveAndContinueAnalysis;
  }
}

async function continueFromStep3() {
  if (!editor || !state.networkPath) {
    toast(L.toast.noNetwork);
    return;
  }
  const findings = editor.validate();
  if (findings.some((f) => f.level === "error")) {
    toast(L.toast.fixValidation);
    updateStep3Continue(findings);
    return;
  }
  try {
    if (!isStepComplete(3)) {
      const data = editor.getData();
      await api("/api/network", {
        method: "POST",
        body: {
          network_path: data.network_path,
          network: data.network,
          catalogue: data.catalogue,
          catalogue_path: data.catalogue_path,
        },
      });
      state.network = data.network;
      state.catalogue = data.catalogue;
      markStepDone(3);
      toast(L.toast.saved);
    }
    goToStep(4);
  } catch (err) {
    toast(err.message || "Could not save network inputs");
  }
}

async function saveNetwork() {
  if (!editor || !state.networkPath) {
    toast(L.toast.noNetwork);
    return;
  }
  const findings = editor.validate();
  if (findings.some((f) => f.level === "error")) {
    toast(L.toast.fixValidation);
    return;
  }
  try {
    const data = editor.getData();
    await api("/api/network", {
      method: "POST",
      body: {
        network_path: data.network_path,
        network: data.network,
        catalogue: data.catalogue,
        catalogue_path: data.catalogue_path,
      },
    });
    state.network = data.network;
    state.catalogue = data.catalogue;
    toast(L.toast.saved);
    markStepDone(3);
    updateStep3Continue();
  } catch (err) {
    toast(err.message || "Could not save network inputs");
  }
}

function getRunOptions() {
  const focus = document.querySelector('input[name="focus"]:checked')?.value || "access";
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const baseName = (state.networkPath || "demo").split("/").pop().replace(".json", "");
  const defaultOut = `results/${baseName}-${ts}`;
  const output = document.getElementById("output-dir").value.trim() || defaultOut;
  return {
    network: state.networkPath,
    catalogue: state.cataloguePath,
    focus,
    output,
    partition_by_admin_area: document.getElementById("opt-partition").checked,
    admin_area_level: document.getElementById("opt-admin-level").value,
    allow_synthetic_referrals: document.getElementById("opt-synthetic").checked,
    quiet: document.getElementById("opt-quiet").checked,
    verbose: document.getElementById("opt-verbose").checked,
  };
}

function buildCommandPreview(mode) {
  const opts = getRunOptions();
  const parts = ["dno"];
  if (mode === "demo") {
    parts.push("demo", "-g", "referral-network", "-o", opts.output);
  } else if (mode === "init") {
    parts.push("init", "./my-network", "-g", "referral-network");
  } else {
    parts.push("optimize", "-n", opts.network);
    if (opts.catalogue) parts.push("-c", opts.catalogue);
    parts.push("-g", "referral-network", "--focus", opts.focus);
    if (opts.partition_by_admin_area) {
      parts.push("--partition-by-admin-area");
      if (opts.admin_area_level) parts.push("--admin-area-level", opts.admin_area_level);
    }
    if (opts.allow_synthetic_referrals) parts.push("--allow-synthetic-referrals");
    if (opts.quiet) parts.push("-q");
    if (opts.verbose) parts.push("-v");
    if (mode === "check") parts.push("--check-only");
    if (mode === "baseline") parts.push("--baseline-only");
    parts.push("-o", opts.output);
  }
  return parts.join(" ");
}

function updateCommandPreview() {
  const out = document.getElementById("output-dir");
  if (!out.value && state.networkPath) {
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const baseName = state.networkPath.split("/").pop().replace(".json", "");
    out.value = `results/${baseName}-${ts}`;
  }
  document.getElementById("command-preview").textContent = buildCommandPreview("optimize");
}

async function runJob(action, options, onDone) {
  clearLog();
  appendLog(`Starting ${action}...`);
  const { job_id, argv } = await api("/api/run", { method: "POST", body: { action, options } });
  state.currentJobId = job_id;
  appendLog(`$ ${argv.join(" ")}`);

  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/stream?job=${job_id}&token=${encodeURIComponent(state.token)}`);

  eventSource.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "line") appendLog(msg.text);
    if (msg.type === "done") {
      eventSource.close();
      eventSource = null;
      state.currentJobId = null;
      const code = msg.exit_code;
      if (code === 0) {
        appendLog("Completed successfully.");
        toast(L.toast.runComplete);
        if (onDone) onDone(options);
      } else {
        appendLog(`Finished with exit code ${code}.`);
        toast(code === 1 ? L.toast.runFailed : L.toast.runError);
      }
      refreshStatus();
    }
  };
}

async function runSetup() {
  const setupBtn = document.getElementById("btn-setup");
  const repairBtn = document.getElementById("btn-repair");
  setupBtn.disabled = true;
  repairBtn.disabled = true;
  setupBtn.textContent = "Installing…";
  clearLog();
  document.getElementById("log-console").classList.remove("collapsed");
  document.getElementById("btn-toggle-log").textContent = "Hide";
  appendLog("Setting up virtual environment and installing packages...");
  const { job_id, argv } = await api("/api/setup", { method: "POST", body: {} });
  state.currentJobId = job_id;
  appendLog(`$ ${argv.join(" ")}`);

  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/stream?job=${job_id}&token=${encodeURIComponent(state.token)}`);
  eventSource.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "line") appendLog(msg.text);
    if (msg.type === "done") {
      eventSource.close();
      eventSource = null;
      state.currentJobId = null;
      setupBtn.disabled = false;
      repairBtn.disabled = false;
      setupBtn.textContent = "Install everything";
      refreshStatus().then((s) => {
        if (s.ready) {
          toast("DNO tools installed — you're ready to conduct analyses");
          markStepDone(1);
        } else {
          toast("Setup finished but something still needs attention — see activity log");
        }
      });
    }
  };
}

function refreshRunHistory(runs) {
  const container = document.getElementById("run-history");
  const list = runs || [];
  if (!list.length) {
    container.innerHTML = `<p class='hint'>${L.results.noRuns}</p>`;
    return;
  }
  container.innerHTML = list.map((r) => {
    const tags = [
      r.has_result ? L.results.tags.optimised : null,
      r.has_baseline ? L.results.tags.baseline : null,
      r.has_validation ? L.results.tags.validated : null,
    ].filter(Boolean).join(", ") || "output";
    return `<div class="run-item" data-path="${r.path}">
      <div><strong>${r.id}</strong><div class="run-meta">${tags} · ${new Date(r.modified).toLocaleString()}</div></div>
      <span>&rarr;</span>
    </div>`;
  }).join("");

  container.querySelectorAll(".run-item").forEach((item) => {
    item.addEventListener("click", () => loadResults(item.dataset.path));
  });
}

async function loadResults(runPath) {
  state.currentRunPath = runPath;
  document.getElementById("results-empty").classList.add("hidden");
  document.getElementById("results-content").classList.remove("hidden");

  let result = null;
  let summary = "";
  let validation = "";
  let baseline = null;

  try {
    const res = await api(`/api/file?path=${encodeURIComponent(runPath + "/result.json")}`);
    result = await res.json();
  } catch { /* baseline-only run */ }

  try {
    const res = await api(`/api/file?path=${encodeURIComponent(runPath + "/summary.md")}`);
    summary = await res.text();
  } catch { /* */ }

  try {
    const res = await api(`/api/file?path=${encodeURIComponent(runPath + "/validation-report.md")}`);
    validation = await res.text();
  } catch { /* */ }

  try {
    const res = await api(`/api/file?path=${encodeURIComponent(runPath + "/baseline.json")}`);
    baseline = await res.json();
  } catch { /* */ }

  renderScorecard(result, baseline);
  renderActions(result, runPath);
  renderMarkdownTab("summary", summary || (baseline ? "Baseline analysis complete — see Baseline metrics tab." : "No decision brief available."));
  state._validationMd = validation;
  state._baselineJson = baseline;
  state._summaryMd = summary;
  markStepDone(4);
  markStepDone(5);
  goToStep(5);
}

function renderScorecard(result, baseline) {
  const grid = document.getElementById("scorecard-grid");
  const deltas = document.getElementById("metrics-deltas");

  if (!result && baseline) {
    grid.innerHTML = `<div class="score-card"><div class="score-value">—</div><div class="score-label">${L.results.baselineOnly}</div></div>`;
    deltas.innerHTML = formatBaselineDeltas(baseline);
    return;
  }

  if (!result) {
    grid.innerHTML = "<p class='hint'>No result.json in this run folder.</p>";
    deltas.innerHTML = "";
    return;
  }

  const m3 = result.matrix_3x3 || {};
  const cards = [
    { label: L.results.scorecard.overall, value: m3.overall_score },
    { label: L.results.scorecard.financial, value: m3.financial?.score },
    { label: L.results.scorecard.programmatic, value: m3.programmatic?.score },
    { label: L.results.scorecard.impact, value: m3.impact?.score ?? m3.impact?.overall_score },
  ];
  grid.innerHTML = cards.map((c) =>
    `<div class="score-card"><div class="score-value">${c.value != null ? Math.round(c.value) : "—"}</div><div class="score-label">${c.label}</div></div>`
  ).join("");

  const bm = result.metrics?.baseline || {};
  const fm = result.metrics?.final || {};
  deltas.innerHTML = formatMetricDeltas(bm, fm, result.actions?.total || 0);
}

function formatMetricDeltas(bm, fm, actionCount) {
  const rows = [
    { label: L.results.metrics.utilisation, b: bm.overall_utilization ?? bm.utilization, f: fm.overall_utilization ?? fm.utilization, lowerBetter: false },
    { label: L.results.metrics.overUtilised, b: bm.overutilized_count ?? bm.overutilized_labs, f: fm.overutilized_count ?? fm.overutilized_labs, lowerBetter: true },
    { label: L.results.metrics.capacity, b: bm.total_capacity, f: fm.total_capacity, lowerBetter: false },
    { label: L.results.metrics.changes, b: 0, f: actionCount, lowerBetter: false },
  ];
  return rows.map((r) => {
    const b = r.b ?? "—";
    const f = r.f ?? "—";
    let cls = "";
    if (typeof b === "number" && typeof f === "number") {
      const improved = r.lowerBetter ? f < b : f > b;
      cls = improved ? "delta-improved" : f !== b ? "delta-worse" : "";
    }
    return `<div class="delta-row"><span>${r.label}</span><span class="${cls}">${b} → ${f}</span></div>`;
  }).join("");
}

function formatBaselineDeltas(baseline) {
  const m = baseline.metrics || baseline;
  const rows = Object.entries(m).slice(0, 8).map(([k, v]) =>
    `<div class="delta-row"><span>${k.replace(/_/g, " ")}</span><span>${typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(2)) : v}</span></div>`
  );
  return rows.join("") || "<p class='hint'>No baseline metrics found.</p>";
}

function renderActions(result, runPath) {
  const wrap = document.getElementById("actions-table-wrap");
  state._actionsCsvPath = runPath + "/actions.csv";

  const actions = result?.actions_list || [];
  if (!actions.length) {
    wrap.innerHTML = `<p class='hint'>${L.results.noActions}</p>`;
    return;
  }

  const redesigns = actions.filter((a) => a.action_type === "REDESIGN" || a.action_type === "redesign" || !a.action_type);
  const rows = redesigns.length ? redesigns : actions;

  let html = `<table class="data-table"><thead><tr>
    <th>${L.results.table.hf}</th><th>${L.results.table.fromLab}</th><th>${L.results.table.toLab}</th><th>${L.results.table.volume}</th><th>${L.results.table.distance}</th>
  </tr></thead><tbody>`;
  rows.forEach((a) => {
    html += `<tr>
      <td>${esc(a.facility_name || a.facility_id || "")}</td>
      <td>${esc(a.old_lab_name || a.source_lab_name || a.old_lab_id || "")}</td>
      <td>${esc(a.new_lab_name || a.target_lab_name || a.new_lab_id || "")}</td>
      <td>${a.volume ?? a.referral_count ?? ""}</td>
      <td>${a.distance_change_km ?? ""}</td>
    </tr>`;
  });
  html += "</tbody></table>";
  wrap.innerHTML = html;
}

function renderMarkdownTab(tab, content) {
  const el = document.getElementById("results-markdown");
  document.querySelectorAll(".results-tab").forEach((t) => t.classList.toggle("active", t.dataset.results === tab));
  if (tab === "baseline" && state._baselineJson) {
    el.innerHTML = `<pre>${esc(JSON.stringify(state._baselineJson, null, 2))}</pre>`;
  } else if (tab === "validation" && state._validationMd) {
    el.innerHTML = renderMarkdown(state._validationMd);
  } else {
    el.innerHTML = renderMarkdown(content || state._summaryMd || "");
  }
}

function renderInline(text) {
  let html = esc(text);
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  return html;
}

function renderMarkdownTable(lines) {
  const rows = lines.filter((line) => line.trim().startsWith("|"));
  if (rows.length < 2) return null;
  const parseRow = (line) =>
    line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const header = parseRow(rows[0]);
  let bodyStart = 1;
  if (/^\|[\s\-:|]+\|$/.test(rows[1]?.trim() || "")) bodyStart = 2;
  const body = rows.slice(bodyStart).map(parseRow);
  let html = "<table><thead><tr>";
  header.forEach((cell) => { html += `<th>${renderInline(cell)}</th>`; });
  html += "</tr></thead><tbody>";
  body.forEach((row) => {
    html += "<tr>";
    row.forEach((cell) => { html += `<td>${renderInline(cell)}</td>`; });
    html += "</tr>";
  });
  html += "</tbody></table>";
  return html;
}

function renderMarkdownBlock(block) {
  if (!block) return "";
  const lines = block.split("\n");
  const table = renderMarkdownTable(lines);
  if (table) return table;
  if (lines.every((line) => /^- /.test(line))) {
    return `<ul>${lines.map((line) => `<li>${renderInline(line.slice(2))}</li>`).join("")}</ul>`;
  }
  if (/^### /.test(block)) return `<h3>${renderInline(block.slice(4))}</h3>`;
  if (/^## /.test(block)) return `<h2>${renderInline(block.slice(3))}</h2>`;
  if (/^# /.test(block)) return `<h1>${renderInline(block.slice(2))}</h1>`;
  return `<p>${lines.map((line) => renderInline(line)).join("<br>")}</p>`;
}

function renderMarkdown(md) {
  if (!md) return "<p class='hint'>No content.</p>";
  return md
    .replace(/\r\n/g, "\n")
    .trim()
    .split(/\n{2,}/)
    .map((block) => renderMarkdownBlock(block.trim()))
    .join("");
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderCapabilityCheck(tier) {
  const tierClass = tier === "free" ? "cap-check--free" : "cap-check--premium";
  return `<span class="cap-check ${tierClass}" aria-hidden="true"><svg viewBox="0 0 16 16" width="16" height="16" fill="none"><path d="M3.5 8.2 6.4 11 12.5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`;
}

function renderCapabilityDash() {
  return `<span class="cap-dash" aria-hidden="true">—</span>`;
}

function renderCapabilityTable(features, c, { compact = false } = {}) {
  const tableClass = compact ? "cap-table cap-table--compact" : "cap-table";
  const rows = features
    .map((item) => {
      const rowClass = item.openSource ? "cap-table-row" : "cap-table-row cap-table-row--premium-only";
      const premiumTag = item.openSource
        ? ""
        : `<span class="cap-feature-tag">${esc(c.premiumTierBadge)}</span>`;
      return `<tr class="${rowClass}">
        <th scope="row" class="cap-table-feature">
          <span class="cap-feature-title">${esc(item.title)}${premiumTag}</span>
          ${compact ? "" : `<span class="cap-feature-desc">${esc(item.desc)}</span>`}
        </th>
        <td class="cap-table-cell cap-table-cell--free">
          ${item.openSource ? renderCapabilityCheck("free") : renderCapabilityDash()}
          <span class="sr-only">${item.openSource ? "Included" : "Not included"}</span>
        </td>
        <td class="cap-table-cell cap-table-cell--premium">
          ${renderCapabilityCheck("premium")}
          <span class="sr-only">Included</span>
        </td>
      </tr>`;
    })
    .join("");

  return `<div class="cap-table-wrap">
    <table class="${tableClass}">
      <thead>
        <tr>
          <th scope="col" class="cap-table-head cap-table-head--feature">${esc(c.tableFeatureCol)}</th>
          <th scope="col" class="cap-table-head cap-table-head--free">
            <span class="cap-table-head-label">${esc(c.tableOpenSourceCol)}</span>
            <span class="tier-badge tier-badge--free">${esc(c.freeTierBadge)}</span>
          </th>
          <th scope="col" class="cap-table-head cap-table-head--premium">
            <span class="cap-table-head-label">${esc(c.tablePlatformCol)}</span>
            <span class="tier-badge tier-badge--premium">${esc(c.premiumTierBadge)}</span>
          </th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function renderCapabilities() {
  const c = L.capabilities;
  const main = document.getElementById("dno-capabilities");
  if (main) {
    main.innerHTML = `
      <header class="capabilities-header">
        <p class="capabilities-eyebrow">${esc(c.eyebrow)}</p>
        <h3 class="capabilities-title" id="capabilities-title">${esc(c.title)}</h3>
        <p class="capabilities-lead">${esc(c.lead)}</p>
      </header>
      ${renderCapabilityTable(c.features, c)}
      <footer class="capabilities-table-footer">
        <img class="froncort-logo capabilities-footer-logo" src="ui/assets/Froncort_SHI_RGB.svg" alt="Froncort">
        <a class="capabilities-platform-cta" href="${esc(c.platformDemoUrl)}" target="_blank" rel="noopener noreferrer">
          ${esc(c.platformCta)} <span aria-hidden="true">→</span>
        </a>
      </footer>`;
  }

  const compact = document.getElementById("dno-capabilities-step4");
  if (compact) {
    compact.innerHTML = `
      <div class="capabilities-compact-panel">
        <div class="capabilities-compact-top">
          <div class="capabilities-compact-header">
            <p class="capabilities-compact-eyebrow">${esc(c.compactEyebrow)}</p>
            <h3 class="capabilities-compact-title" id="capabilities-compact-title">${esc(c.compactTitle)}</h3>
            <p class="capabilities-compact-lead">${esc(c.compactLead)}</p>
          </div>
        </div>
        ${renderCapabilityTable(c.features, c, { compact: true })}
        <div class="capabilities-compact-cta">
          <a class="capabilities-platform-title-link" href="${esc(c.platformDemoUrl)}" target="_blank" rel="noopener noreferrer">${esc(c.platformCta)} →</a>
        </div>
      </div>`;
  }
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el && text != null) el.textContent = text;
}

function applyStep2Copy() {
  const d = L.data;
  const step = L.steps[2];
  setText("step-2-title", step.title);
  setText("step-2-lead", step.lead);
  setText("load-path-hint", d.loadPathHint);
  setText("btn-load-path", d.load);
  setText("btn-use-demo", d.useDemo);
  setText("load-demo-note", d.demoNote);
  setText("step-2-continue-msg", d.scenarioReady);
  setText("btn-go-edit", d.continueToInputs);
}

function applyRunStepCopy() {
  const r = L.run;
  setText("run-focus-title", r.focusTitle);
  setText("run-advanced-summary", r.advancedSummary);
  setText("run-scope-title", r.scopeTitle);
  setText("run-partition-label", r.partition);
  setText("run-partition-hint", r.partitionHint);
  setText("run-admin-level-label", r.adminLevel);
  setText("run-srs-title", r.srsTitle);
  setText("run-synthetic-label", r.synthetic);
  setText("run-synthetic-hint", r.syntheticHint);
  setText("run-log-title", r.logTitle);
  setText("run-quiet-label", r.quiet);
  setText("run-quiet-hint", r.quietHint);
  setText("run-verbose-label", r.verbose);
  setText("run-verbose-hint", r.verboseHint);
  setText("run-output-label", r.output);
  setText("run-output-hint", r.outputHint);
  setText("run-command-label", r.command);

  const adminSelect = document.getElementById("opt-admin-level");
  if (adminSelect?.options[0]) adminSelect.options[0].textContent = r.adminArea1;
  if (adminSelect?.options[1]) adminSelect.options[1].textContent = r.adminArea2;

  const focusCards = document.querySelectorAll("#focus-cards .focus-card");
  const focuses = ["access", "util", "cost", "balanced"];
  focusCards.forEach((card, i) => {
    const key = focuses[i];
    const copy = r[key];
    if (!copy) return;
    const title = card.querySelector(".focus-title");
    const desc = card.querySelector(".focus-desc");
    if (title) title.textContent = copy.title;
    if (desc) desc.textContent = copy.desc;
  });

  setText("btn-check", r.validate);
  setText("btn-baseline", r.baseline);
  setText("btn-optimize", r.optimize);
  setText("btn-demo", r.demo);
  applyStep2Copy();
}

function syncPartitionAdminLevel() {
  const enabled = document.getElementById("opt-partition")?.checked;
  const select = document.getElementById("opt-admin-level");
  const wrap = document.getElementById("admin-level-wrap");
  if (select) select.disabled = !enabled;
  if (wrap) wrap.classList.toggle("is-disabled", !enabled);
}

function bindEvents() {
  document.querySelectorAll(".step-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const step = parseInt(tab.dataset.step, 10);
      goToStep(step);
    });
  });

  document.getElementById("btn-setup").addEventListener("click", runSetup);
  document.getElementById("btn-repair").addEventListener("click", runSetup);
  document.getElementById("btn-continue-setup").addEventListener("click", () => goToStep(2));
  document.getElementById("btn-remove-venv").addEventListener("click", async () => {
    if (!confirm("Remove the virtual environment? You will need to set up again.")) return;
    await api("/api/remove-venv", { method: "POST", body: {} });
    toast("Environment removed");
    refreshStatus();
  });

  document.getElementById("btn-load-path").addEventListener("click", async () => {
    state.networkPath = document.getElementById("network-path-input").value.trim();
    state.cataloguePath = document.getElementById("catalogue-path-input").value.trim() || null;
    if (!state.networkPath) { toast(L.toast.enterPath); return; }
    try {
      await loadNetwork();
      showSelectedNetwork();
      markStepDone(2);
    } catch (err) {
      toast(err.message || "Could not load network");
    }
  });

  document.getElementById("btn-use-demo").addEventListener("click", () => {
    loadDemoSample().catch((err) => toast(err.message));
  });

  document.getElementById("btn-go-edit").addEventListener("click", () => goToStep(3));
  document.getElementById("btn-go-run")?.addEventListener("click", () => continueFromStep3());

  document.querySelectorAll(".editor-tab").forEach((tab) => {
    tab.addEventListener("click", () => editor?.setTab(tab.dataset.editor));
  });
  document.getElementById("editor-search")?.addEventListener("input", (e) => editor?.setSearch(e.target.value));
  document.getElementById("btn-save-network").addEventListener("click", saveNetwork);

  document.querySelectorAll('input[name="focus"], #opt-partition, #opt-admin-level, #opt-synthetic, #opt-quiet, #opt-verbose, #output-dir')
    .forEach((el) => el.addEventListener("change", updateCommandPreview));
  document.getElementById("opt-partition")?.addEventListener("change", syncPartitionAdminLevel);
  document.getElementById("output-dir").addEventListener("input", updateCommandPreview);

  document.getElementById("btn-copy-command").addEventListener("click", () => {
    navigator.clipboard.writeText(document.getElementById("command-preview").textContent);
    toast("Command copied");
  });

  document.getElementById("btn-check").addEventListener("click", () => {
    const opts = { ...getRunOptions(), check_only: true };
    runJob("optimize", opts, (o) => { state.currentRunPath = o.output; loadResults(o.output); markStepDone(4); });
  });

  document.getElementById("btn-baseline").addEventListener("click", () => {
    const opts = { ...getRunOptions(), baseline_only: true };
    runJob("optimize", opts, (o) => { state.currentRunPath = o.output; loadResults(o.output); markStepDone(4); });
  });

  document.getElementById("btn-optimize").addEventListener("click", () => {
    if (!state.networkPath) { toast(L.toast.chooseNetwork); return; }
    const opts = getRunOptions();
    runJob("optimize", opts, (o) => { state.currentRunPath = o.output; loadResults(o.output); markStepDone(4); });
  });

  document.getElementById("btn-demo").addEventListener("click", () => {
    const opts = { output: `results/demo-${Date.now()}` };
    runJob("demo", opts, (o) => { state.currentRunPath = o.output; loadResults(o.output); });
  });

  document.querySelectorAll(".results-tab").forEach((tab) => {
    tab.addEventListener("click", () => renderMarkdownTab(tab.dataset.results));
  });

  document.getElementById("btn-reveal-folder").addEventListener("click", async () => {
    if (!state.currentRunPath) return;
    await api("/api/reveal", { method: "POST", body: { path: state.currentRunPath } });
  });

  document.getElementById("btn-download-zip").addEventListener("click", () => {
    if (!state.currentRunPath) return;
    window.open(`/api/zip?path=${encodeURIComponent(state.currentRunPath)}&token=${encodeURIComponent(state.token)}`);
  });

  document.getElementById("btn-download-actions").addEventListener("click", () => {
    if (state._actionsCsvPath) {
      window.open(`/api/file?path=${encodeURIComponent(state._actionsCsvPath)}&token=${encodeURIComponent(state.token)}`);
    }
  });

  document.getElementById("btn-copy-log").addEventListener("click", () => {
    navigator.clipboard.writeText(document.getElementById("log-body").textContent);
    toast("Log copied");
  });

  document.getElementById("btn-cancel-job").addEventListener("click", async () => {
    if (!state.currentJobId) return;
    await api("/api/cancel", { method: "POST", body: { job_id: state.currentJobId } });
    toast("Cancel requested");
  });

  document.getElementById("btn-toggle-log").addEventListener("click", (e) => {
    e.stopPropagation();
    const log = document.getElementById("log-console");
    const btn = document.getElementById("btn-toggle-log");
    log.classList.toggle("collapsed");
    btn.textContent = log.classList.contains("collapsed") ? "Show" : "Hide";
  });

  document.querySelector(".log-header").addEventListener("click", (e) => {
    if (e.target.closest(".log-actions")) return;
    const log = document.getElementById("log-console");
    const btn = document.getElementById("btn-toggle-log");
    log.classList.toggle("collapsed");
    btn.textContent = log.classList.contains("collapsed") ? "Show" : "Hide";
  });

  const sidebar = document.getElementById("sidebar");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebarToggleLabel = sidebarToggle.querySelector(".sidebar-toggle-label");
  sidebarToggle.addEventListener("click", () => {
    const collapsed = sidebar.classList.toggle("collapsed");
    sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
    sidebarToggle.setAttribute("aria-label", collapsed ? "Show sidebar" : "Collapse sidebar");
    sidebarToggle.title = collapsed ? "Show sidebar" : "";
    sidebarToggleLabel.textContent = collapsed ? "Show" : "Collapse";
  });
}

async function init() {
  editor = new NetworkEditor("editor-content", "validation-banner");
  editor.onValidationChange = (findings) => updateStep3Continue(findings);
  renderCapabilities();
  applyRunStepCopy();
  syncPartitionAdminLevel();
  bindEvents();
  try {
    await refreshStatus();
  } catch (err) {
    appendLog("Error: " + err.message);
    appendLog("Start the UI with: python3 dno_ui.py");
  }
}

document.addEventListener("DOMContentLoaded", init);
