# Preparing a network for AI-DNO Open Source

This guide explains how to describe your diagnostic network in the format AI-DNO Open Source reads. It is written for programme and data teams; no programming is required to prepare the input.

For a complete UI and CLI operating guide, see **[walkthrough.md](walkthrough.md)**. For CSV upload column headers used in the local editor, see [walkthrough.md — Step 3](walkthrough.md#step-3--prepare-network-inputs).

A network is described in JSON — a plain-text format of labelled fields. You can edit it in any text editor or map data from a structured spreadsheet export. The optimizer reads a fixed set of field names; the friendlier programme terms (referring site, testing site, sample referral) appear in the reports it produces.

Throughout this guide:

- a **referring facility** is a site where samples originate (a health facility or sample collection centre);
- a **testing site** (or **laboratory**) is where samples are processed on a device;
- a **sample referral** is the linkage that carries samples from a referring facility to a testing site.

---

## If your data is in Excel

AI-DNO Open Source does **not** read Excel workbooks (.xlsx, .xls). The local UI accepts **CSV only** in the Prepare inputs step.

If your programme data lives in an Excel workbook, save each sheet as a separate CSV before upload:

| Excel sheet (typical name) | Save as CSV | Upload in UI tab |
| -------------------------- | ----------- | ---------------- |
| Health facilities | `health-facilities.csv` | **Health facilities** |
| Diagnostic services / testing sites | `testing-sites.csv` | **Testing sites** |
| Sample referrals | `sample-referral-linkages.csv` | **Sample referral linkages** |

**Steps in Excel:** open the sheet → **File → Save As** → **CSV (Comma delimited) (*.csv)** → save. Repeat for each sheet.

For CLI runs, merge the CSV data into `network.json` (or use the UI editor to import CSVs and save). JSON remains the canonical format for `dno optimize`.

---

## Files

A network is described primarily by **`network.json`**. An optional **`network.catalogue.json`** may sit beside it when you need device capacity or cost parameters. Open Referral optimization does not require a catalogue.

```text
my-network/
├── network.json              Sites, devices, demand, and referral linkages
└── network.catalogue.json    Optional — device/test parameters
```

| Scenario | Files needed |
|----------|--------------|
| `referral-network` (Open Referral) | `network.json` |

Scenario and objective focus are explained in the [README](../README.md#analysis-choices).

---

## The two demand quantities

A diagnostic network carries two related but distinct quantities. Keeping them separate is what allows the optimizer to reason about both demand and sample flow.

| Field | Programme term | Meaning |
|-------|----------------|---------|
| `test_volumes` | Annual testing demand | Tests generated at a referring facility (where demand originates) |
| `referrals[].referral_count` | Annual samples referred | Volume carried on a single facility → laboratory linkage |

Testing demand describes how much testing a facility needs; the referral count describes how much is actually sent along a particular linkage. A referral is the bridge between the two.

---

## `network.json`

### Referring facilities — `facilities[]`

Health facilities and sample collection centres where specimens originate. In the current model these sites do not perform on-site molecular testing.

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Unique identifier |
| `name` | Yes | Human-readable name |
| `latitude`, `longitude` | Yes | Geographic coordinates |
| `admin_area_1`, `admin_area_2` | Optional | State / district, used when optimizing within administrative boundaries |

### Testing sites — `labs[]`

Laboratories and testing sites that host devices. Same field structure as referring facilities.

### Devices — `devices[]`

Each device belongs to a **testing site** through its `facility_id`, which must match an entry in `labs[]`.

| Field | Notes |
|-------|-------|
| `device_id` | Unique identifier |
| `device_type` | Diagnostic device type, for example GeneXpert or TrueNAT |
| `max_shifts`, `max_tests_per_shift` | Operating assumptions used to compute capacity |
| `device_type_id` | Links the device to the catalogue when a catalogue is present |

Testing capacity is derived from operating hours, shifts, and staffing — not from a device's theoretical maximum alone.

### Annual testing demand — `test_volumes`

The number of tests generated per year at each referring facility.

```json
"test_volumes": { "Facility A": 1200, "Facility B": 900 }
```

### Sample referral linkages — `referrals[]`

Each entry records the current linkage from one referring facility to one testing site.

```json
{
  "facility_id": "Facility A",
  "lab_id": "District Laboratory",
  "referral_count": 1200,
  "distance_km": 8.5
}
```

- `referral_count` is the annual number of samples sent along this linkage.
- In the current model, each referring facility has one active testing-site linkage.

### Tests offered at a laboratory — `manual_capability_overrides`

Declares which tests each laboratory is able to run. This is helpful when modelling capacity across multiple tests and optional for minimal referral-only networks.

```json
"manual_capability_overrides": [["District Laboratory", "tt-mtb"]]
```

---

## `network.catalogue.json` (optional)

The catalogue defines device types and, for each, the tests they run and the parameters used for capacity and cost. Open Referral runs do not require it; include it when your data contains device economics for baseline cost breakdown.

```json
{
  "device_types": [
    {
      "id": "dt-gx4",
      "name": "GeneXpert (4-module)",
      "test_parameters": [
        {
          "test_type_id": "tt-mtb",
          "max_tests_per_shift": 40,
          "max_shifts": 2,
          "cost_per_test": 12.5
        }
      ]
    }
  ]
}
```

---

## Reference network — sample demo

The repository ships a synthetic demo at `samples/demo/` for learning and quick verification:

```bash
dno demo -o results/demo/
dno optimize -n samples/demo/network.json -c samples/demo/network.catalogue.json -g referral-network -o results/demo/
```

All names and coordinates are synthetic. See `samples/demo/README.md` for what the scenario demonstrates.

---

## Preparing your programme's network

When you have your own structured export or facility master list, map it to `network.json` following the field guide below. **`dno init` is a JSON skeleton for that mapping work** — not a substitute for real programme data.

```bash
dno init ./my-network --goal referral-network
```

Replace every ID, coordinate, and volume with your data. Then validate before optimizing:

```bash
dno optimize -n ./my-network/network.json --check-only
```

---

## Validating the input

`optimize` validates the input automatically before the solver runs. To validate without running the solver — useful while preparing data — use `--check-only`:

```bash
dno optimize -n my-network/network.json --check-only
dno optimize -n my-network/network.json --check-only -o validation-report.md
```

Validation findings identify the exact location of each issue (for example `referrals[2].lab_id`) so it can be corrected quickly.

---

## Preparation checklist

The following points reflect how the model interprets the data. Confirming them before a run avoids misleading results.

| Point | What to confirm |
|-------|-----------------|
| Two demand quantities | `test_volumes` holds annual testing demand; `referral_count` holds per-linkage sample volume. |
| Device ownership | Each device's `facility_id` points to a **testing site** in `labs[]`, not to a referring facility. |
| Referral coverage | Facilities with demand have a referral linkage; where they do not, run `optimize` with `--allow-synthetic-referrals`. |
| Laboratory capabilities | `manual_capability_overrides` lists tests each lab runs when using multi-test capacity logic. |

---

## How actions appear in the reports

Open Referral emits one action type: **sample referral change** (`REDESIGN`).

The mathematical basis is documented in [algorithms.md](algorithms.md).
