# AI-DNO Open Source — optimization algorithms

This document explains the mathematical formulation and algorithmic logic used by the Open Referral solver, evaluators, and scoring functions included in AI-DNO Open Source. It is intended for analysts and engineers who need to understand or verify how recommendations are produced.

For programme context — what Diagnostic Network Optimization is and how to run analyses — see the [README](../README.md) and [walkthrough](walkthrough.md). For the input format, see [data preparation](data-preparation.md).

## How the algorithms map to the domain

The package implements referral redesign used in Open Referral DNO exercises: access, utilization, and cost are weighted in the MILP objective, and results are summarized with a 3×3 scorecard.

The user-facing controls map onto the solver as follows:

- **Scenario** (`--goal`): Open Referral only — MILP referral redesign (`redesign` mode).
- **Focus** (`--focus`) sets the objective weights `(utilization, accessibility, cost)` applied inside the MILP objective.

This package covers referral redesign. For wider planning capabilities, see [AI-DNO Platform](../README.md#ai-dno-platform).

---

## Table of Contents

- [Hierarchical Solver](#hierarchical-solver)
- [Phase 1 — MILP Referral Redesign](#phase-1--milp-referral-redesign)
- [Matrix 3x3 Scoring](#matrix-3x3-scoring)
  - [Financial Axis](#financial-axis)
  - [Programmatic Axis](#programmatic-axis)
  - [Impact Axis](#impact-axis)
  - [Overall Score](#overall-score)
- [RRE Score](#rre-score)
- [Utilization](#utilization)
- [Accessibility](#accessibility)
- [Synthetic Referral Scoring](#synthetic-referral-scoring)

---

## Hierarchical Solver

`src/dno_core/solvers/hierarchical.py`

The AI-DNO Open Source solver runs **Phase 1 — MILP referral redesign** only. `DynamicScenarioConfig.optimization_modes` must be `["redesign"]` (via `OssGoal(preset="open_referral")`).

```
Baseline evaluation
      │
      ▼
Phase 1 — MILP Redesign
  repeat until convergence or max_iterations:
    ├── MILPDecompositionSolver (referral reassignment)
    ├── Convergence check (utilization Δ < tolerance, cost Δ < threshold)
    └── Oscillation detection (5-iteration rolling window)
      │
      ▼
Final scoring
  └── Matrix3x3Evaluator
```

**Convergence conditions (Phase 1):**

- Utilization change between iterations < `utilization_tolerance` for `min_convergence_iterations` consecutive iterations
- Cost change < `cost_improvement_threshold`
- Oscillation detected: if the same utilization value recurs within the last `oscillation_detection_window` iterations

**Admin-area-wise mode:** When `DynamicScenarioConfig.optimize_by_admin_area` is `True`, the state is partitioned by `admin_area_1` or `admin_area_2`. The full solver runs independently on each partition. All resulting actions are merged and reapplied to the full state for final evaluation.

---

## Phase 1 — MILP Referral Redesign

`src/dno_core/solvers/milp_decomposition.py`

Reassigns non-lab facilities to labs via Mixed-Integer Linear Programming. Optimizes the referral network within administrative boundaries.

### Subgraph Construction

The problem is decomposed into one subgraph per administrative boundary (`admin_area_1` or `admin_area_2`, controlled by `admin_boundary_level`). Each subgraph contains the facilities and labs within one boundary. Subgraphs are solved in parallel. Maximum subgraph size is capped at `milp_max_subgraph_size` (default: 300; the solver enforces a minimum of 300).

### Decision Variables

```
x[f, l] ∈ {0, 1}    1 if facility f is assigned to lab l, else 0
```

### Objective Function

Minimize the total weighted assignment cost:

```
minimize Σ_{f,l} cost(f, l) × x[f, l]   [+ M × asd_slack  if ASD regression guard active]
```

**`cost(f, l)`** is a composite penalty per facility-lab pair:

```
distance_penalty = min(dist(f, l) / 2, 100)

financial_cost   = dist(f, l) × cost_per_km(route)    # per-route cpk or config fallback
norm_cost        = min(financial_cost / 2000, 1) × 100

# Utilization: penalty if assigning f to l would overload l; reward if l is underloaded
projected_util   = (current_demand_l + volume_f) / capacity_l
util_component   = (piecewise — see below)

# ASD boost: assignments that reduce average sample distance are favored
beyond_asd_boost = 1.5  if dist(f, l) > subgraph_mean_distance  else 1.0

# Population weight: higher-volume facilities receive proportional weight
pop_weight       = clamp(volume_f / mean_volume, 0.1, 10)

cost(f, l) = (w_dist × distance_penalty
            + w_util × util_component
            + w_cost × norm_cost)
           × beyond_asd_boost
           × pop_weight
```

**Utilization component (piecewise):**

```
if projected_util < 0.6:   util_component = -20 × (1 - projected_util)   # reward underloaded
if projected_util > 0.9:   util_component = +80 × (projected_util - 0.9) # penalize overload
else:                       util_component = linear interpolation
```

**Weight mapping from user-facing `(utilization, accessibility, cost)` to internal `(w_util, w_dist, w_cost)`:**

User weights are normalised to sum to 1, then:

```
if utilization_weight ≤ 0.3:   w_util ≈ 0.1  (ASD-dominant mode)
if utilization_weight ≥ 0.6:   w_util ≈ 0.5  (utilization-dominant mode)
else:                           linear blend
```

### Constraints

1. **Assignment:** Each facility is assigned to exactly one lab:
   ```
   Σ_l x[f, l] = 1   for all f
   ```

2. **Capacity:** Lab demand does not exceed effective capacity (with configurable overload limit):
   ```
   Σ_f volume_f × x[f, l] ≤ effective_capacity_l   for all l
   ```
   `effective_capacity_l` subtracts base lab demand, out-of-subgraph committed volume, and co-located volume. Default cap: `max_lab_utilization` × `lab_capacity` (0.95 by default, relaxed in ASD-focused mode).

3. **Co-location locks:** Facilities co-located with a lab are locked to that lab (`x[f, l] = 1`).

4. **ASD regression guard** (if `milp_allow_asd_regression = False`):
   ```
   Σ_{f,l} volume_f × dist(f,l) × x[f,l] ≤ baseline_weighted_distance + asd_slack + ε
   objective += M × asd_slack    where M = milp_asd_violation_penalty (default: 5 × 10⁶)
   ```

### Merge and Filter

After solving each subgraph, results are merged back into a global assignment diff. Two filters apply:

- **Minimum distance gain:** Assignment changes with `distance_change_km < milp_min_distance_gain_km` (default: 1.0 km) are dropped, unless the move relieves an overloaded lab.
- **Global capacity guard:** Moves are sorted by distance change descending; any move that would push the destination lab above 100% utilization is dropped.

---

## Matrix 3x3 Scoring

`src/dno_core/evaluators/matrix_3x3.py`

Final composite scoring across three axes. Evaluated on the optimized baseline (after applying redesign actions) vs the current baseline.

### Financial Axis

```
transport_savings   = baseline_sample_transport_cost - optimized_sample_transport_cost
annual_device_inv   = 0  # Open Referral does not add devices
net_annual_benefit  = transport_savings - annual_device_inv

if baseline_transport_cost > 0:
    annual_score = clamp(70 + (net_annual_benefit / baseline_transport_cost) × 20, 0, 100)
    score = clamp(annual_score - 0.15 × max(0, cost_delta_percent), 0, 100)
else:
    score = 50 - cost_delta_percent
```

### Programmatic Axis

```
score = clamp(80 - min(40, 2.5 × action_count) + compliance_bonus, 0, 100)

compliance_bonus = +30  if no policy violations
                   0    otherwise

policy violation: cross-boundary redesign action when allow_cross_boundary = False
```

### Impact Axis

```
capacity_score     = clamp(50 + 2 × capacity_change_pct, 0, 100)
utilization_score  = max(0, 100 - 10 × final_overutilized_lab_count)
asd_improvement    = (asd_baseline - asd_final) / asd_baseline
rre_points         = clamp(rre_score × 333, 0, 100)

impact_score = 0.25 × capacity_score
             + 0.30 × utilization_score
             + 0.25 × asd_improvement_score
             + 0.20 × rre_points
```

### Overall Score

```
overall = 0.33 × financial + 0.33 × programmatic + 0.34 × impact
```

Weights are adjustable; the 0.34 bias toward impact reflects the primary optimization intent.

---

## RRE Score

`src/dno_core/evaluators/rre.py`

Measures total sample transport distance improvement across the network.

```
baseline_total   = Σ_f  dist(f, assigned_lab_baseline)  × volume_f
optimized_total  = Σ_f  dist(f, assigned_lab_optimized) × volume_f

total_distance_improvement = (baseline_total - optimized_total) / baseline_total
asd_improvement            = (asd_baseline - asd_optimized) / asd_baseline
  where asd = total / n_facilities

raw_rre_score = max(0, 0.7 × total_distance_improvement + 0.3 × asd_improvement)
distance_saved_km = baseline_total - optimized_total
```

Per-admin-area and per-`admin_area_2` breakdowns are also computed.

---

## Utilization

`src/dno_core/evaluators/utilization.py`

Per lab:

```
u(lab) = (base_demand_lab + Σ_{referrals to lab} volume_facility) / capacity_lab
```

Network-level (capacity-weighted):

```
u_network = Σ_l u(l) × capacity_l  /  Σ_l capacity_l
```

Thresholds from `EngineConfig`:

- Overutilized: `u > utilization_threshold_high` (default: 1.00)
- Underutilized: `u < utilization_threshold_low` (default: 0.40)

---

## Accessibility

`src/dno_core/evaluators/accessibility.py`

For each non-lab facility with coordinates and a referral:

```
d_f = dist(facility, assigned_lab)     # haversine with circuitry factor

asd = (Σ_f d_f × volume_f) / (Σ_f volume_f)    # volume-weighted average sample distance
```

Accessibility score (0–100):

```
distance_score  = max(0, 100 - asd)
coverage_score  = 0.20 × pct_facilities_within_10km
                + 0.50 × pct_facilities_within_25km
                + 0.30 × pct_facilities_within_50km

accessibility_score = 0.70 × distance_score + 0.30 × coverage_score
```

Per-facility component used in MILP:

```
accessibility_component(f) = 1 / (1 + d_f²)
```

---

## Synthetic Referral Scoring

`src/dno_core/domain/referral_generator.py`

Used when no transport route data exists. For each non-lab facility, scores all labs and assigns the top `max_referrals_per_facility`:

```
score(facility, lab) = w_dist × e^(-dist/50)
                     + w_cap  × (1 - mean_utilization_lab)
                     + w_adm  × admin_score
                     + w_tier × tier_score

admin_score:
  same admin_area_2 → 1.0
  same admin_area_1 → 0.5
  otherwise         → 0.2

tier_score:
  lab.tier_level ≥ facility.tier_level → 1.0 (proportional otherwise)
```

Default weights: `w_dist = 0.4`, `w_cap = 0.3`, `w_adm = 0.2`, `w_tier = 0.1` (from `EngineConfig`).

Distance is raw Haversine (R = 6371 km, no circuitry factor) for relative ranking.
