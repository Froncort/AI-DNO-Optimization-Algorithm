# Copyright 2026 Froncort
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Per-device shift-time MILP capacity constraints (Q.1) for mixed-test labs.

Single-test subgraphs keep the legacy scalar constraint in milp_decomposition.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import pulp

from dno_core.calculators.shift_capacity import (
    ALL_TEST_BUCKET,
    active_tests_at_lab,
    device_shift_budget,
    lab_has_mixed_test_devices,
    mtps_for_device_type,
    supported_tests_for_device_type,
    tests_currently_running_on_device,
)
from dno_core.models.facility import Facility
from dno_core.models.network_state import NetworkState


def facility_demand_by_test(state: NetworkState, facility_id: str) -> Dict[str, float]:
    """Per-test annual demand for one facility (aggregate fallback when no split)."""
    fid = str(facility_id)
    if state.test_volumes_by_type and fid in state.test_volumes_by_type:
        return {str(t): float(v or 0) for t, v in state.test_volumes_by_type[fid].items() if int(v or 0) > 0}
    total = float(state.test_volumes.get(fid, 0) or 0)
    if total > 0:
        return {ALL_TEST_BUCKET: total}
    return {}


def subgraph_active_tests(
    state: NetworkState,
    facilities: List[Facility],
    labs: List[Facility],
) -> Set[str]:
    """Union of test ids with positive demand in this subgraph."""
    tests: Set[str] = set()
    for f in facilities:
        tests.update(facility_demand_by_test(state, f.id).keys())
    for lab in labs:
        tests.update(facility_demand_by_test(state, lab.id).keys())
    return tests


def subgraph_single_test_fast_path(
    state: NetworkState,
    facilities: List[Facility],
    labs: List[Facility],
) -> bool:
    """True when every demand row in the subgraph maps to one active test bucket."""
    tests = subgraph_active_tests(state, facilities, labs)
    if not tests:
        return True
    if len(tests) == 1 and ALL_TEST_BUCKET in tests:
        return True
    return len(tests) <= 1


def lab_has_capability_for_test(state: NetworkState, lab_id: str, test_id: str) -> bool:
    """Lab can process test t when at least one device contributes budget for t."""
    from dno_core.calculators.shift_capacity import per_test_capacity_at_lab

    wd = state.config.working_days_per_year if state.config else 300
    caps = per_test_capacity_at_lab(state, lab_id, wd)
    if test_id == ALL_TEST_BUCKET:
        return sum(caps.values()) > 0
    return caps.get(test_id, 0.0) > 0


def add_capability_gates(
    prob: pulp.LpProblem,
    x: Dict[Tuple[str, str], pulp.LpVariable],
    state: NetworkState,
    facilities: List[Facility],
    labs: List[Facility],
    current_assignments: Optional[Dict[str, str]] = None,
    locked_pairs: Optional[Set[Tuple[str, str]]] = None,
) -> int:
    """
    Forbid x[f,l] when facility f has demand for test t but lab l cannot run t.

    R.2 feasibility guards (each is mandatory to avoid an infeasible Sum_l x[f,l]==1):
      1. Never gate a locked (f,l) pair (self-referral / co-located lock) — a capability
         gap at a locked facility is an integration trigger, not a routing prohibition.
      2. No-cover fallback: if NO in-subgraph lab can cover all of f's demanded tests,
         do not gate f at all (the capable lab may be outside this admin subgraph).
      3. Per-facility feasibility: only emit gates when >=1 capable lab remains assignable.
    """
    current_assignments = current_assignments or {}
    locked_pairs = locked_pairs or set()
    gates = 0
    for f in facilities:
        by_test = facility_demand_by_test(state, f.id)
        if not by_test:
            continue
        demanded = [t for t, v in by_test.items() if v > 0]
        if not demanded:
            continue

        capable_labs = [
            lab for lab in labs
            if all(lab_has_capability_for_test(state, lab.id, t) for t in demanded)
        ]
        # Guard 2/3: no in-subgraph lab can serve all tests → leave f ungated.
        if not capable_labs:
            continue

        cur = current_assignments.get(f.id)
        for lab in labs:
            if lab in capable_labs:
                continue
            # Guard 1: never gate a locked or current-assignment pair.
            if (f.id, lab.id) in locked_pairs or cur == lab.id:
                continue
            prob += x[(f.id, lab.id)] == 0
            gates += 1
    return gates


def precommitted_per_test_demand(
    state: NetworkState,
    lab_id: str,
    facilities: List[Facility],
    colocated_fac_ids: Set[str],
    subgraph_facility_ids: Set[str],
    current_assignments: Dict[str, str],
) -> Dict[str, float]:
    """
    Fixed per-test load at lab l not driven by MILP x (base, out-of-subgraph, colocated lock).
    """
    out: Dict[str, float] = {}

    # Lab's own base demand per test
    if state.test_volumes_by_type and lab_id in state.test_volumes_by_type:
        for tid, vol in state.test_volumes_by_type[lab_id].items():
            if int(vol or 0) > 0:
                out[str(tid)] = out.get(str(tid), 0.0) + float(vol)
    else:
        base = float(state.test_volumes.get(lab_id, 0) or 0)
        if base > 0:
            out[ALL_TEST_BUCKET] = out.get(ALL_TEST_BUCKET, 0.0) + base

    # Co-located locked facilities at this lab
    for fid in colocated_fac_ids:
        for tid, vol in facility_demand_by_test(state, fid).items():
            if vol > 0:
                out[tid] = out.get(tid, 0.0) + vol

    # Out-of-subgraph referrals already assigned here
    for ref in state.referrals:
        if ref.lab_id != lab_id:
            continue
        fid = ref.facility_id
        if fid in subgraph_facility_ids or fid in colocated_fac_ids:
            continue
        for tid, vol in facility_demand_by_test(state, fid).items():
            if vol > 0:
                out[tid] = out.get(tid, 0.0) + vol

    # In-subgraph facilities currently assigned here (fixed baseline before MILP moves)
    for f in facilities:
        if f.id in colocated_fac_ids:
            continue
        if current_assignments.get(f.id) != lab_id:
            continue
        for tid, vol in facility_demand_by_test(state, f.id).items():
            if vol > 0:
                out[tid] = out.get(tid, 0.0) + vol

    return out


def _devices_at_lab(state: NetworkState, lab_id: str) -> List[Any]:
    return [d for d in state.devices.values() if d.facility_id == lab_id]


def lab_y_eligibility(
    state: NetworkState,
    lab_id: str,
    precommitted: Dict[str, float],
    cap_l: float,
    working_days: int,
) -> Tuple[bool, Optional[str]]:
    """
    Pre-check fixed demand can be packed before adding C1/C2 equalities.
    """
    if not lab_has_mixed_test_devices(state, lab_id):
        return False, "not_mixed_test_lab"

    params = state.device_test_parameters_by_type or {}
    active = set(active_tests_at_lab(state, lab_id))
    devices = _devices_at_lab(state, lab_id)
    if not devices:
        return False, "locked_capability_gap"

    for test_id, vol in precommitted.items():
        if vol <= 0:
            continue
        if test_id != ALL_TEST_BUCKET and test_id not in active:
            return False, "locked_capability_gap"
        capable = False
        for dev in devices:
            dt_id = str(dev.device_type_id or "")
            running = tests_currently_running_on_device(state, dev.device_id)
            if test_id == ALL_TEST_BUCKET:
                if dev.max_tests_per_shift > 0 and (
                    ALL_TEST_BUCKET in running or not state.device_type_test_volumes
                ):
                    capable = True
                    break
            elif test_id in running:
                capable = True
                break
            elif not state.device_type_test_volumes:
                sup = supported_tests_for_device_type(dt_id, params)
                if test_id in sup:
                    capable = True
                    break
        if not capable:
            return False, "locked_capability_gap"

    # Greedy pack check for precommitted load within cap_l * device budgets
    load: Dict[str, float] = {d.device_id: 0.0 for d in devices}
    for test_id, vol in sorted(precommitted.items(), key=lambda kv: kv[0]):
        need = float(vol)
        if need <= 0:
            continue
        for dev in devices:
            if need <= 0:
                break
            dt_id = str(dev.device_type_id or "")
            if test_id == ALL_TEST_BUCKET:
                mtps = max(1, int(dev.max_tests_per_shift or 1))
            else:
                mtps = mtps_for_device_type(dt_id, test_id, params)
            if mtps <= 0:
                continue
            budget = device_shift_budget(dev, working_days) * cap_l
            spare = budget - load[dev.device_id]
            if spare <= 0:
                continue
            take = min(need, spare * mtps)
            load[dev.device_id] += take / float(mtps)
            need -= take
        if need > 1e-3:
            return False, "unpackable_precommitted"

    return True, None


def add_lab_per_device_y_constraints(
    prob: pulp.LpProblem,
    x: Dict[Tuple[str, str], pulp.LpVariable],
    state: NetworkState,
    lab: Facility,
    facilities: List[Facility],
    precommitted: Dict[str, float],
    cap_l: float,
    y_vars: Dict[Tuple[str, str, str], pulp.LpVariable],
) -> None:
    """
    Add C1 per-test coverage and C2 per-device shift budgets for one mixed lab.
    """
    working_days = state.config.working_days_per_year if state.config else 300
    params = state.device_test_parameters_by_type or {}
    active = set(active_tests_at_lab(state, lab.id))
    devices = _devices_at_lab(state, lab.id)

    # Collect tests that appear in assigned or precommitted demand
    tests: Set[str] = set(precommitted.keys())
    for f in facilities:
        tests.update(facility_demand_by_test(state, f.id).keys())
    tests = {t for t in tests if t != ALL_TEST_BUCKET or len(tests) == 1}

    for test_id in tests:
        if test_id != ALL_TEST_BUCKET and test_id not in active:
            continue

        lhs_terms = []
        for dev in devices:
            dt_id = str(dev.device_type_id or "")
            if test_id == ALL_TEST_BUCKET:
                mtps = max(1, int(dev.max_tests_per_shift or 1))
            else:
                mtps = mtps_for_device_type(dt_id, test_id, params)
                running = tests_currently_running_on_device(state, dev.device_id)
                if test_id not in running:
                    if state.device_type_test_volumes:
                        continue
                    sup = supported_tests_for_device_type(dt_id, params)
                    if test_id not in sup:
                        continue
            if mtps <= 0:
                continue
            key = (lab.id, dev.device_id, test_id)
            if key not in y_vars:
                y_vars[key] = pulp.LpVariable(
                    f"y_{lab.id[:6]}_{dev.device_id[:6]}_{test_id[:6]}",
                    lowBound=0,
                )
            lhs_terms.append(y_vars[key])

        assigned_terms = []
        for f in facilities:
            vol_map = facility_demand_by_test(state, f.id)
            vol = vol_map.get(test_id, 0.0)
            if vol <= 0:
                continue
            assigned_terms.append(vol * x[(f.id, lab.id)])

        pre = float(precommitted.get(test_id, 0.0))
        if lhs_terms:
            prob += pulp.lpSum(lhs_terms) == pulp.lpSum(assigned_terms) + pre

    for dev in devices:
        dt_id = str(dev.device_type_id or "")
        budget = device_shift_budget(dev, working_days) * cap_l
        if budget <= 0:
            continue
        load_terms = []
        sup = supported_tests_for_device_type(dt_id, params)
        running = tests_currently_running_on_device(state, dev.device_id)
        # C2 budget: running-only when historical activity exists; legacy union with catalogue
        if state.device_type_test_volumes:
            test_ids = set(running)
        else:
            test_ids = set(sup) | set(running)
        if not test_ids and dev.max_tests_per_shift > 0:
            test_ids = {ALL_TEST_BUCKET}
        for test_id in test_ids:
            if test_id != ALL_TEST_BUCKET and test_id not in active:
                continue
            if test_id == ALL_TEST_BUCKET:
                mtps = max(1, int(dev.max_tests_per_shift or 1))
            else:
                mtps = mtps_for_device_type(dt_id, test_id, params)
            if mtps <= 0:
                continue
            key = (lab.id, dev.device_id, test_id)
            if key not in y_vars:
                y_vars[key] = pulp.LpVariable(
                    f"y_{lab.id[:6]}_{dev.device_id[:6]}_{test_id[:6]}",
                    lowBound=0,
                )
            load_terms.append(y_vars[key] / float(mtps))
        if load_terms:
            prob += pulp.lpSum(load_terms) <= budget
