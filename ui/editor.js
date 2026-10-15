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

/* DNO Network Editor — guided table editor with live validation */

const KNOWN_TOP_KEYS = new Set([
  "schema_version", "project_id", "facilities", "labs", "devices", "referrals",
  "transport_routes", "test_volumes", "test_volumes_by_type", "device_type_volumes",
  "device_type_test_volumes", "device_active_tests", "test_type_names",
  "use_historical_lab_baseline", "config", "goal_kind", "manual_capability_overrides",
]);

const FACILITY_KEYS = new Set([
  "id", "name", "facility_tier", "infrastructure_level", "sector_type",
  "admin_area_1", "admin_area_2", "latitude", "longitude", "is_lab", "is_hub",
  "is_active", "hr_technician_count", "operational_shifts", "population",
]);

const DEVICE_KEYS = new Set([
  "facility_id", "device_type", "device_id", "device_type_id", "quantity",
  "max_shifts", "max_tests_per_shift", "stored_annual_capacity", "modules",
  "cost_per_test", "overhead_cost_annual", "shift_cost_annual", "startup_cost",
  "status", "current_utilization", "tests_conducted_annual",
]);

const REFERRAL_KEYS = new Set([
  "facility_id", "lab_id", "referral_count", "distance_km", "is_synthetic", "confidence",
]);

const COLUMN_LABELS = {
  id: "ID",
  name: "Name",
  latitude: "Latitude",
  longitude: "Longitude",
  admin_area_1: "Admin area 1",
  admin_area_2: "Admin area 2",
  annual_demand: "Annual testing demand",
  device_id: "Device ID",
  facility_id: "Health facility ID",
  lab_id: "Testing site ID",
  device_type: "Device type",
  max_shifts: "Max shifts",
  max_tests_per_shift: "Max tests/shift",
  referral_count: "Referral volume",
  distance_km: "Distance (km)",
  test_type_id: "Test type ID",
  cost_per_test: "Cost/test",
};

const CSV_TEMPLATE_FILES = {
  labs: "testing-sites-template.csv",
  facilities: "health-facilities-template.csv",
  devices: "diagnostic-devices-template.csv",
  referrals: "sample-referral-linkages-template.csv",
  catalogue: "device-catalogue-template.csv",
};

const CATALOGUE_CSV_COLS = [
  "id", "name", "test_type_id", "max_tests_per_shift", "max_shifts", "cost_per_test",
];

function columnLabel(col) {
  return COLUMN_LABELS[col] || col;
}

function normalizeHeader(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function parseCsvLine(line) {
  const result = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"' && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        current += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      result.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  result.push(current.trim());
  return result;
}

function resolveCsvColumn(header, cols) {
  const raw = String(header || "").trim();
  if (!raw) return null;
  const direct = cols.find((col) => col === raw);
  if (direct) return direct;
  const normalized = normalizeHeader(raw);
  const normalizedMatch = cols.find((col) => normalizeHeader(col) === normalized);
  if (normalizedMatch) return normalizedMatch;
  return cols.find((col) => normalizeHeader(columnLabel(col)) === normalized) || null;
}

function escapeCsvValue(value) {
  const text = value == null ? "" : String(value);
  if (/[",\n\r]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

const ERROR_MESSAGES = {
  duplicate_device_id: "Two diagnostic devices share the same ID — each device needs a unique ID.",
  device_facility_not_lab: "A diagnostic device is assigned to a health facility, but devices must belong to a testing site.",
  referral_unknown_lab: "A sample referral linkage points to a testing site that does not exist.",
  referral_unknown_facility: "A sample referral linkage points to a health facility that does not exist.",
  multiple_referrals_per_hf: "A health facility has more than one sample referral linkage — only one testing site per facility is supported.",
  test_volume_unknown_hf: "Annual testing demand is listed for a health facility that is not in your network.",
  demand_without_referral: "A health facility has testing demand but no sample referral linkage to a testing site.",
  labs_required: "You need at least one testing site in your diagnostic network.",
  devices_empty: "No diagnostic devices declared — testing site capacity will be zero.",
  unknown_keys: "The file contains field names the DNO optimiser does not recognise.",
};

class NetworkEditor {
  constructor(containerId, bannerId) {
    this.container = document.getElementById(containerId);
    this.banner = document.getElementById(bannerId);
    this.network = null;
    this.catalogue = null;
    this.networkPath = null;
    this.cataloguePath = null;
    this.activeTab = "project";
    this.searchQuery = "";
    this.onChange = null;
    this.onValidationChange = null;
    this._pendingCsvImport = null;
  }

  load(network, catalogue, networkPath, cataloguePath) {
    this.network = network;
    this.catalogue = catalogue || { device_types: [] };
    this.networkPath = networkPath;
    this.cataloguePath = cataloguePath;
    if (!this.network.test_volumes) this.network.test_volumes = {};
    if (!this.network.facilities) this.network.facilities = [];
    if (!this.network.labs) this.network.labs = [];
    if (!this.network.devices) this.network.devices = [];
    if (!this.network.referrals) this.network.referrals = [];
    this.render();
    this.validate();
  }

  getData() {
    return {
      network: this.sanitizeNetwork(),
      catalogue: this.catalogue,
      network_path: this.networkPath,
      catalogue_path: this.cataloguePath,
    };
  }

  sanitizeNetwork() {
    const n = JSON.parse(JSON.stringify(this.network));
    const out = {};
    for (const key of KNOWN_TOP_KEYS) {
      if (n[key] !== undefined && n[key] !== null) {
        out[key] = n[key];
      }
    }
    if (!out.schema_version) out.schema_version = 1;
    if (!out.test_volumes) out.test_volumes = {};
    if (!out.facilities) out.facilities = [];
    if (!out.labs) out.labs = [];
    if (!out.devices) out.devices = [];
    if (!out.referrals) out.referrals = [];
    return out;
  }

  validate() {
    const findings = [];
    const n = this.network;
    const labIds = new Set((n.labs || []).map((l) => l.id));
    const facIds = new Set((n.facilities || []).map((f) => f.id));
    const deviceIds = new Set();

    for (const key of Object.keys(n)) {
      if (!KNOWN_TOP_KEYS.has(key)) {
        findings.push({ level: "error", check: "unknown_keys", path: key, message: `Unknown field "${key}"` });
      }
    }

    if (!n.labs || n.labs.length === 0) {
      findings.push({ level: "error", check: "labs_required", path: "labs", message: "At least one testing site required" });
    }

    (n.devices || []).forEach((d, i) => {
      if (deviceIds.has(d.device_id)) {
        findings.push({ level: "error", check: "duplicate_device_id", path: `devices[${i}]`, message: `Duplicate device_id ${d.device_id}` });
      }
      deviceIds.add(d.device_id);
      if (d.facility_id && !labIds.has(d.facility_id)) {
        findings.push({ level: "error", check: "device_facility_not_lab", path: `devices[${i}]`, message: `${d.facility_id} is not a lab` });
      }
    });

    if (!n.devices || n.devices.length === 0) {
      findings.push({ level: "warning", check: "devices_empty", path: "devices", message: "No devices declared" });
    }

    const refByFac = {};
    (n.referrals || []).forEach((r, i) => {
      if (r.lab_id && !labIds.has(r.lab_id)) {
        findings.push({ level: "error", check: "referral_unknown_lab", path: `referrals[${i}]`, message: `Unknown lab ${r.lab_id}` });
      }
      if (r.facility_id && !facIds.has(r.facility_id)) {
        findings.push({ level: "error", check: "referral_unknown_facility", path: `referrals[${i}]`, message: `Unknown facility ${r.facility_id}` });
      }
      if (r.facility_id) {
        refByFac[r.facility_id] = (refByFac[r.facility_id] || 0) + 1;
      }
    });

    for (const [fid, count] of Object.entries(refByFac)) {
      if (count > 1) {
        findings.push({ level: "error", check: "multiple_referrals_per_hf", path: `referrals (${fid})`, message: `Multiple referrals for ${fid}` });
      }
    }

    for (const [hfId, vol] of Object.entries(n.test_volumes || {})) {
      if (!facIds.has(hfId)) {
        findings.push({ level: "error", check: "test_volume_unknown_hf", path: `test_volumes.${hfId}`, message: `Unknown facility ${hfId}` });
      } else if (vol > 0 && !refByFac[hfId]) {
        findings.push({ level: "error", check: "demand_without_referral", path: `test_volumes.${hfId}`, message: `Demand without referral for ${hfId}` });
      }
    }

    this._findings = findings;
    this._renderBanner(findings);
    return findings;
  }

  _renderBanner(findings) {
    const errors = findings.filter((f) => f.level === "error");
    const warnings = findings.filter((f) => f.level === "warning");
    this.banner.className = "validation-banner show";
    if (errors.length === 0 && warnings.length === 0) {
      this.banner.classList.add("ok");
      this.banner.textContent = "All checks passed — network inputs are ready for DNO analysis.";
    } else if (errors.length > 0) {
      this.banner.classList.add("err");
      this.banner.innerHTML = errors
        .slice(0, 5)
        .map((f) => `<div><strong>${ERROR_MESSAGES[f.check] || f.message}</strong> <code>${f.path}</code></div>`)
        .join("");
    } else {
      this.banner.classList.add("warn");
      this.banner.innerHTML = warnings.map((f) => `<div>${f.message}</div>`).join("");
    }
    if (this.onValidationChange) this.onValidationChange(findings);
  }

  setTab(tab) {
    this.activeTab = tab;
    document.querySelectorAll(".editor-tab").forEach((el) => {
      el.classList.toggle("active", el.dataset.editor === tab);
    });
    this.render();
  }

  setSearch(q) {
    this.searchQuery = (q || "").toLowerCase();
    this.render();
  }

  render() {
    if (!this.network) {
      this.container.innerHTML = "<p class='empty-state'>No scenario loaded. Go to step 2 to select diagnostic network data.</p>";
      return;
    }
    switch (this.activeTab) {
      case "project": this._renderProject(); break;
      case "labs": this._renderEntityTable("labs", ["id", "name", "latitude", "longitude", "admin_area_1", "admin_area_2"]); break;
      case "facilities": this._renderFacilities(); break;
      case "devices": this._renderDevices(); break;
      case "referrals": this._renderReferrals(); break;
      case "catalogue": this._renderCatalogue(); break;
      case "advanced": this._renderAdvanced(); break;
      default: this._renderProject();
    }
  }

  _renderProject() {
    this.container.innerHTML = `
      <div class="card">
        <label class="field-label">Scenario ID
          <input type="text" class="text-input" id="ed-project-id" value="${this._esc(this.network.project_id || "")}">
        </label>
        <label class="field-label">Schema version
          <input type="text" class="text-input" value="1" disabled>
        </label>
        <p class="hint">Network file: <code>${this._esc(this.networkPath || "")}</code></p>
      </div>`;
    document.getElementById("ed-project-id").addEventListener("change", (e) => {
      this.network.project_id = e.target.value;
      this._notify();
    });
  }

  _renderFacilities() {
    const cols = ["id", "name", "latitude", "longitude", "admin_area_1", "admin_area_2", "annual_demand"];
    this._renderTableShell("facilities", cols, (row, col, idx) => {
      if (col === "annual_demand") {
        const vol = (this.network.test_volumes || {})[row.id] || 0;
        return `<input type="number" data-field="annual_demand" data-idx="${idx}" value="${vol}" min="0">`;
      }
      return `<input type="text" data-field="${col}" data-idx="${idx}" value="${this._esc(row[col] ?? "")}">`;
    }, () => ({
      id: `hf-${Date.now()}`, name: "New health facility", latitude: "", longitude: "",
      admin_area_1: "", admin_area_2: "",
    }));
    this._bindFacilityDemand();
  }

  _bindFacilityDemand() {
    this.container.querySelectorAll('[data-field="annual_demand"]').forEach((inp) => {
      inp.addEventListener("change", (e) => {
        const idx = parseInt(e.target.dataset.idx, 10);
        const fac = this.network.facilities[idx];
        if (!this.network.test_volumes) this.network.test_volumes = {};
        const val = parseInt(e.target.value, 10) || 0;
        if (val > 0) this.network.test_volumes[fac.id] = val;
        else delete this.network.test_volumes[fac.id];
        this.validate();
        this._notify();
      });
    });
  }

  _renderDevices() {
    const labIds = (this.network.labs || []).map((l) => l.id);
    const cols = ["device_id", "facility_id", "device_type", "max_shifts", "max_tests_per_shift"];
    this._renderTableShell("devices", cols, (row, col, idx) => {
      if (col === "facility_id") {
        const opts = labIds.map((id) => `<option value="${this._esc(id)}" ${row.facility_id === id ? "selected" : ""}>${this._esc(id)}</option>`).join("");
        return `<select data-field="${col}" data-idx="${idx}">${opts}</select>`;
      }
      return `<input type="${col.includes("max_") ? "number" : "text"}" data-field="${col}" data-idx="${idx}" value="${this._esc(row[col] ?? "")}">`;
    }, () => ({
      device_id: `dev-${Date.now()}`, facility_id: labIds[0] || "", device_type: "GX 4",
      max_shifts: 2, max_tests_per_shift: 40,
    }));
  }

  _renderReferrals() {
    const cols = ["facility_id", "lab_id", "referral_count", "distance_km"];
    this._renderTableShell("referrals", cols, (row, col, idx) => {
      if (col === "referral_count" || col === "distance_km") {
        return `<input type="number" data-field="${col}" data-idx="${idx}" value="${row[col] ?? 0}" step="any">`;
      }
      // Text inputs — avoids O(rows × IDs) select options on large networks.
      return `<input type="text" data-field="${col}" data-idx="${idx}" value="${this._esc(row[col] ?? "")}">`;
    }, () => ({
      facility_id: (this.network.facilities || [])[0]?.id || "",
      lab_id: (this.network.labs || [])[0]?.id || "",
      referral_count: 0,
      distance_km: 0,
    }));
  }

  _renderCatalogue() {
    if (!this.catalogue) this.catalogue = { device_types: [] };
    const types = this.catalogue.device_types || [];
    let html = `<div class="table-toolbar">
      <button type="button" class="btn btn-secondary btn-sm" id="btn-add-device-type">Add device type</button>
      <button type="button" class="btn btn-ghost btn-sm" id="btn-upload-csv">Upload CSV</button>
      <button type="button" class="btn btn-ghost btn-sm" id="btn-download-template">Download template</button>
    </div><div class="table-wrap"><table class="data-table"><thead><tr>
      <th>Device type ID</th><th>Name</th><th>Test type ID</th><th>Max tests/shift</th><th>Max shifts</th><th>Cost/test</th><th></th>
    </tr></thead><tbody>`;

    types.forEach((dt, i) => {
      const tp = (dt.test_parameters || [])[0] || {};
      html += `<tr data-cat-idx="${i}">
        <td><input type="text" data-cat-field="id" value="${this._esc(dt.id || "")}"></td>
        <td><input type="text" data-cat-field="name" value="${this._esc(dt.name || "")}"></td>
        <td><input type="text" data-cat-field="test_type_id" value="${this._esc(tp.test_type_id || "")}"></td>
        <td><input type="number" data-cat-field="max_tests_per_shift" value="${tp.max_tests_per_shift || 0}"></td>
        <td><input type="number" data-cat-field="max_shifts" value="${tp.max_shifts || 1}"></td>
        <td><input type="number" data-cat-field="cost_per_test" value="${tp.cost_per_test || 0}" step="any"></td>
        <td><button type="button" class="btn btn-ghost btn-sm btn-del-cat" data-idx="${i}">Delete</button></td>
      </tr>`;
    });
    html += "</tbody></table></div>";
    this.container.innerHTML = html;

    document.getElementById("btn-add-device-type")?.addEventListener("click", () => {
      types.push({ id: `dt-${Date.now()}`, name: "New device", test_parameters: [{ test_type_id: "tt-tb", max_tests_per_shift: 40, max_shifts: 2, cost_per_test: 0 }] });
      this.render();
      this._notify();
    });

    this._bindCsvActions("catalogue", CATALOGUE_CSV_COLS);

    this.container.querySelectorAll("[data-cat-field]").forEach((inp) => {
      inp.addEventListener("change", (e) => {
        const row = e.target.closest("tr");
        const idx = parseInt(row.dataset.catIdx, 10);
        const field = e.target.dataset.catField;
        const dt = types[idx];
        if (["id", "name"].includes(field)) {
          dt[field] = e.target.value;
        } else {
          if (!dt.test_parameters) dt.test_parameters = [{}];
          if (!dt.test_parameters[0]) dt.test_parameters[0] = {};
          dt.test_parameters[0][field] = field.includes("cost") ? parseFloat(e.target.value) : parseInt(e.target.value, 10);
        }
        this._notify();
      });
    });

    this.container.querySelectorAll(".btn-del-cat").forEach((btn) => {
      btn.addEventListener("click", () => {
        types.splice(parseInt(btn.dataset.idx, 10), 1);
        this.render();
        this._notify();
      });
    });
  }

  _renderAdvanced() {
    const adv = {
      test_volumes_by_type: this.network.test_volumes_by_type,
      test_type_names: this.network.test_type_names,
      use_historical_lab_baseline: this.network.use_historical_lab_baseline,
      manual_capability_overrides: this.network.manual_capability_overrides,
    };
    this.container.innerHTML = `
      <div class="card">
        <label class="checkbox-label">
          <input type="checkbox" id="adv-historical" ${adv.use_historical_lab_baseline ? "checked" : ""}>
          Use historical testing site baseline
        </label>
        <label class="field-label">Raw JSON (advanced fields)
          <textarea class="raw-json-editor" id="adv-raw-json">${this._esc(JSON.stringify(adv, null, 2))}</textarea>
        </label>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-apply-raw">Apply raw JSON</button>
      </div>
      <div class="card">
        <label class="field-label">Full network JSON (read-only — use Save network inputs to persist table edits)
          <textarea class="raw-json-editor" readonly>${this._esc(JSON.stringify(this.sanitizeNetwork(), null, 2))}</textarea>
        </label>
      </div>`;

    document.getElementById("adv-historical")?.addEventListener("change", (e) => {
      this.network.use_historical_lab_baseline = e.target.checked;
      this._notify();
    });
    document.getElementById("btn-apply-raw")?.addEventListener("click", () => {
      try {
        const parsed = JSON.parse(document.getElementById("adv-raw-json").value);
        Object.assign(this.network, parsed);
        this.validate();
        this.render();
        this._notify();
      } catch (err) {
        alert("Invalid JSON: " + err.message);
      }
    });
  }

  _renderEntityTable(arrayName, cols) {
    this._renderTableShell(arrayName, cols, (row, col, idx) =>
      `<input type="text" data-field="${col}" data-idx="${idx}" value="${this._esc(row[col] ?? "")}">`,
      () => {
        const base = { id: `${arrayName.slice(0, 3)}-${Date.now()}`, name: arrayName === "labs" ? "New testing site" : "New health facility", latitude: "", longitude: "", admin_area_1: "", admin_area_2: "" };
        return base;
      }
    );
  }

  _renderTableShell(arrayName, cols, cellRenderer, newRowFn) {
    const arr = this.network[arrayName] || [];
    const filtered = this.searchQuery
      ? arr.map((row, idx) => ({ row, idx })).filter(({ row }) =>
          cols.some((c) => String(row[c] ?? "").toLowerCase().includes(this.searchQuery))
        )
      : arr.map((row, idx) => ({ row, idx }));

    let html = `<div class="table-toolbar">
      <button type="button" class="btn btn-secondary btn-sm" id="btn-add-row">Add row</button>
      <button type="button" class="btn btn-ghost btn-sm" id="btn-upload-csv">Upload CSV</button>
      <button type="button" class="btn btn-ghost btn-sm" id="btn-download-template">Download template</button>
    </div><div class="table-wrap"><table class="data-table"><thead><tr>`;
    cols.forEach((c) => { html += `<th>${columnLabel(c)}</th>`; });
    html += "<th></th></tr></thead><tbody>";

    filtered.forEach(({ row, idx }) => {
      html += `<tr data-array="${arrayName}" data-idx="${idx}">`;
      cols.forEach((col) => { html += `<td>${cellRenderer(row, col, idx)}</td>`; });
      html += `<td><button type="button" class="btn btn-ghost btn-sm btn-del-row" data-idx="${idx}">Delete</button></td></tr>`;
    });
    html += "</tbody></table></div>";
    this.container.innerHTML = html;

    document.getElementById("btn-add-row")?.addEventListener("click", () => {
      arr.push(newRowFn());
      this.render();
      this.validate();
      this._notify();
    });

    this._bindCsvActions(arrayName, cols);

    this.container.querySelectorAll("input[data-field], select[data-field]").forEach((el) => {
      el.addEventListener("change", (e) => {
        const idx = parseInt(e.target.dataset.idx, 10);
        const field = e.target.dataset.field;
        if (field === "annual_demand") return;
        const val = e.target.type === "number" ? (parseFloat(e.target.value) || 0) : e.target.value;
        arr[idx][field] = val;
        if (arrayName === "facilities" && field === "id") {
          const oldId = Object.keys(this.network.test_volumes || {}).find((k) => this.network.test_volumes[k] && arr[idx].name);
        }
        this.validate();
        this._notify();
      });
    });

    this.container.querySelectorAll(".btn-del-row").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.idx, 10);
        const removed = arr[idx];
        arr.splice(idx, 1);
        if (arrayName === "facilities" && removed?.id) {
          delete (this.network.test_volumes || {})[removed.id];
        }
        this.render();
        this.validate();
        this._notify();
      });
    });

    if (arrayName === "facilities") this._bindFacilityDemand();
  }

  _getCsvInput() {
    if (!this._csvInput) {
      this._csvInput = document.createElement("input");
      this._csvInput.type = "file";
      this._csvInput.accept = ".csv,text/csv";
      this._csvInput.hidden = true;
      document.body.appendChild(this._csvInput);
      this._csvInput.addEventListener("change", () => {
        const file = this._csvInput.files?.[0];
        const pending = this._pendingCsvImport;
        this._csvInput.value = "";
        this._pendingCsvImport = null;
        if (!file || !pending) return;
        const name = file.name.toLowerCase();
        const isCsv =
          name.endsWith(".csv") ||
          file.type === "text/csv" ||
          file.type === "application/csv";
        const isExcel =
          name.endsWith(".xlsx") ||
          name.endsWith(".xls") ||
          file.type === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" ||
          file.type === "application/vnd.ms-excel";
        if (isExcel || !isCsv) {
          const lingo = window.DNO_LINGO?.editor;
          alert(
            isExcel
              ? lingo?.csvExcelRejected ||
                  "Excel files are not supported. Save each sheet as a separate CSV first."
              : "Only CSV files are supported. Save your sheet as CSV (File → Save As → CSV) and try again.",
          );
          return;
        }
        const reader = new FileReader();
        reader.onload = (event) => {
          const text = event.target?.result;
          if (typeof text !== "string" || !text.trim()) return;
          if (pending.mode === "catalogue") {
            this._importCatalogueCsv(text);
          } else {
            this._importCsv(pending.arrayName, pending.cols, text, true);
          }
        };
        reader.onerror = () => {
          const lingo = window.DNO_LINGO?.editor;
          alert(lingo?.csvReadError || "Could not read the CSV file. Please try again.");
        };
        reader.readAsText(file);
      });
    }
    return this._csvInput;
  }

  _bindCsvActions(arrayName, cols) {
    document.getElementById("btn-upload-csv")?.addEventListener("click", () => {
      this._pendingCsvImport = {
        arrayName,
        cols,
        mode: arrayName === "catalogue" ? "catalogue" : "table",
      };
      this._getCsvInput().click();
    });

    document.getElementById("btn-download-template")?.addEventListener("click", () => {
      this._downloadTemplate(cols, CSV_TEMPLATE_FILES[arrayName] || `${arrayName}-template.csv`);
    });
  }

  _downloadTemplate(cols, filename) {
    const header = cols.map((col) => escapeCsvValue(columnLabel(col))).join(",");
    const csv = `${header}\n`;
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  _parseCsvRows(text, cols) {
    const lines = text.trim().split(/\r?\n/).filter((line) => line.trim());
    if (!lines.length) return [];

    const firstCells = parseCsvLine(lines[0]);
    const columnIndexes = {};
    let start = 0;

    firstCells.forEach((header, index) => {
      const col = resolveCsvColumn(header, cols);
      if (col) columnIndexes[index] = col;
    });

    if (Object.keys(columnIndexes).length > 0) {
      start = 1;
    } else {
      cols.forEach((col, index) => {
        columnIndexes[index] = col;
      });
    }

    const rows = [];
    for (let i = start; i < lines.length; i += 1) {
      const parts = parseCsvLine(lines[i]);
      if (!parts.some((part) => part !== "")) continue;
      const row = {};
      Object.entries(columnIndexes).forEach(([index, col]) => {
        row[col] = parts[Number(index)] ?? "";
      });
      if (cols.some((col) => col !== "annual_demand" && String(row[col] ?? "").trim() !== "")) {
        rows.push(row);
      }
    }
    return rows;
  }

  _importCsv(arrayName, cols, text, replace = false) {
    const parsedRows = this._parseCsvRows(text, cols);
    if (!parsedRows.length) {
      alert("No rows found in the CSV file.");
      return;
    }

    const arr = this.network[arrayName];
    if (replace) {
      arr.length = 0;
      if (arrayName === "facilities") {
        this.network.test_volumes = {};
      }
    }

    parsedRows.forEach((parsed) => {
      const row = {};
      cols.forEach((col) => {
        if (col === "annual_demand") return;
        const raw = parsed[col];
        if (col === "max_shifts" || col === "max_tests_per_shift" || col === "referral_count" || col === "distance_km") {
          row[col] = raw === "" ? 0 : Number(raw);
        } else {
          row[col] = raw ?? "";
        }
      });
      arr.push(row);

      if (arrayName === "facilities" && cols.includes("annual_demand")) {
        const vol = parseInt(parsed.annual_demand, 10);
        if (vol > 0 && row.id) {
          if (!this.network.test_volumes) this.network.test_volumes = {};
          this.network.test_volumes[row.id] = vol;
        }
      }
    });

    this.render();
    this.validate();
    this._notify();
  }

  _importCatalogueCsv(text) {
    const parsedRows = this._parseCsvRows(text, CATALOGUE_CSV_COLS);
    if (!parsedRows.length) {
      alert("No rows found in the CSV file.");
      return;
    }

    if (!this.catalogue) this.catalogue = { device_types: [] };
    this.catalogue.device_types = parsedRows.map((parsed) => ({
      id: parsed.id || "",
      name: parsed.name || "",
      test_parameters: [{
        test_type_id: parsed.test_type_id || "",
        max_tests_per_shift: parsed.max_tests_per_shift === "" ? 0 : Number(parsed.max_tests_per_shift),
        max_shifts: parsed.max_shifts === "" ? 1 : Number(parsed.max_shifts),
        cost_per_test: parsed.cost_per_test === "" ? 0 : Number(parsed.cost_per_test),
      }],
    }));

    this.render();
    this._notify();
  }

  _notify() {
    if (this.onChange) this.onChange();
  }

  _esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  }
}

window.NetworkEditor = NetworkEditor;
