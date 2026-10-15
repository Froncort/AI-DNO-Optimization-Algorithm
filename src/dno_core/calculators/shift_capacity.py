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
Shift-equivalent capacity helpers for multi-test DNO.

Per-device shift budgets and per-(device_type, test) throughput drive utilization,
MILP feasibility, and the expansion ladder (integrate -> shift -> device).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from dno_core.calculators.cost import (
    historical_lab_demand_by_test,
    is_device_operational_for_costing,
    is_inferred_baseline_state,
    origin_route_sample_volume_total,
    route_sample_volume,
)
from dno_core.models.device import DeviceInventory
from dno_core.models.network_state import NetworkState

# Sentinel bucket when per-test demand is unavailable (single-test fallback).
ALL_TEST_BUCKET = "ALL"

# Large finite penalty when demand exists but no device can run that test.
CAPABILITY_GAP_UTIL = 9.99

CapabilityKey = Tuple[str, str]  # (lab_id, test_type_id)
ParamKey = Tuple[str, str]  # (device_type_id, test_type_id)


@dataclass
class LabShiftPackingResult:
    """Greedy shift packing at one lab — shared by ρ* and device residual triggers."""

    rho: float
    device_load: Dict[str, float]
    residual_over_budget_by_test: Dict[str, float]
    capability_gap: bool = False
    # Explainability trace: test volume assigned per (device_id, test_id) during packing
    allocated_by_device_test: Dict[Tuple[str, str], float] = None

    def __post_init__(self) -> None:
        if self.allocated_by_device_test is None:
            self.allocated_by_device_test = {}


def _legacy_activity_fallback(state: NetworkState) -> bool:
    """No per-device/test history — keep broad catalogue+active-lab running fallback."""
    return not bool(state.device_type_test_volumes)


def tests_currently_running_on_device(state: NetworkState, device_id: str) -> Set[str]:
    """Tests this device currently processes (explicit active set, not catalogue-only)."""
    dev = state.devices.get(device_id)
    if not dev:
        return set()
    explicit = set((state.device_active_tests or {}).get(device_id, set()))
    if explicit:
        return explicit
    if _legacy_activity_fallback(state):
        params = state.device_test_parameters_by_type or {}
        supported = supported_tests_for_device_type(str(dev.device_type_id or ""), params)
        active_lab = set(active_tests_at_lab(state, dev.facility_id))
        return supported & active_lab
    return explicit


def device_shift_budget(device: DeviceInventory, working_days: int) -> float:
    """Annual shift-units available on one device (shifts per day × working days)."""
    return float(device.max_shifts) * float(working_days)


def mtps_for_device_type(
    device_type_id: str,
    test_type_id: str,
    params_by_type: Optional[Dict[ParamKey, dict]],
) -> int:
    """Max tests per shift for (device_type, test) from catalogue; 0 if unsupported."""
    if not params_by_type or not device_type_id or not test_type_id:
        return 0
    row = params_by_type.get((str(device_type_id), str(test_type_id)))
    if not row:
        return 0
    return max(0, int(row.get("max_tests_per_shift") or 0))


def supported_tests_for_device_type(
    device_type_id: str,
    params_by_type: Optional[Dict[ParamKey, dict]],
) -> Set[str]:
    """Tests a device type can run according to ingest catalogue rows."""
    if not params_by_type or not device_type_id:
        return set()
    out: Set[str] = set()
    for (dt_id, test_id), row in params_by_type.items():
        if str(dt_id) != str(device_type_id):
            continue
        if int(row.get("max_tests_per_shift") or 0) > 0:
            out.add(str(test_id))
    return out


def seed_facility_test_capabilities(
    state: NetworkState,
    db_capabilities: Optional[Set[CapabilityKey]] = None,
) -> Set[CapabilityKey]:
    """
    Build in-memory (lab_id, test_type_id) capability set before scenario replay (R.3.1).

    A lab is active for test ``t`` iff it has >=1 device whose device_type CATALOGUE
    supports ``t`` AND there is an activity signal for ``t`` at that lab — either a
    DB ACTIVE row or positive own/historical volume. Catalogue support alone does NOT
    make a test active; that distinction is exactly what TEST_INTEGRATION activates.
    """
    params = state.device_test_parameters_by_type or {}
    devices_by_lab = state.get_devices_by_lab()

    # Per-lab catalogue support: tests the lab's devices CAN run (integration eligibility).
    lab_catalogue: Dict[str, Set[str]] = {}
    for lab_id, devices in devices_by_lab.items():
        sup: Set[str] = set()
        for dev in devices:
            if not dev.device_type_id:
                continue
            sup |= supported_tests_for_device_type(str(dev.device_type_id), params)
        if sup:
            lab_catalogue[str(lab_id)] = sup

    # Activity signals: DB ACTIVE rows + positive BASELINE own/routed per-test volume.
    # Routed (referred-in) volume is included so pure receiving hubs (devices present,
    # inbound referrals, zero own demand) are not wrongly left test-inactive. This is
    # safe for the integration trigger because seeding runs ONCE on the baseline state
    # (before any redesign hypothesis reroutes demand); hypothesis copies are not
    # re-seeded, so VL rerouted onto a non-VL lab during search still surfaces as overflow.
    db_caps = {(str(a), str(b)) for (a, b) in (db_capabilities or set())}

    caps: Set[CapabilityKey] = set()
    for lab_id, supported in lab_catalogue.items():
        baseline_demand = lab_total_demand_by_test(state, lab_id)
        active_by_volume = {
            str(t) for t, v in baseline_demand.items() if int(v or 0) > 0
        }
        for test_id in supported:
            if (lab_id, test_id) in db_caps or test_id in active_by_volume:
                caps.add((lab_id, test_id))

    state.facility_test_capabilities = caps

    # Seed device_active_tests from historical (facility, device_type, test) volumes.
    type_test_vols = state.device_type_test_volumes or {}
    if type_test_vols:
        if state.device_active_tests is None:
            state.device_active_tests = {}
        for dev in state.devices.values():
            if not is_device_operational_for_costing(dev):
                continue
            fid = str(dev.facility_id)
            dt_id = str(dev.device_type_id or "")
            if not dt_id:
                continue
            for (f, dt, tt), vol in type_test_vols.items():
                if f == fid and dt == dt_id and int(vol or 0) > 0:
                    activate_test_on_device(state, dev.device_id, str(tt))

    return caps


def is_test_active_at_lab(
    state: NetworkState,
    lab_id: str,
    test_type_id: str,
) -> bool:
    """Whether test t is active for capacity/routing at lab l."""
    caps = state.facility_test_capabilities or set()
    if caps:
        return (str(lab_id), str(test_type_id)) in caps
    # TB-only fallback: single aggregate bucket
    return test_type_id == ALL_TEST_BUCKET or bool(state.test_volumes_by_type)


def active_tests_at_lab(state: NetworkState, lab_id: str) -> List[str]:
    """Sorted list of test_type_ids active at a lab."""
    lid = str(lab_id)
    caps = state.facility_test_capabilities or set()
    if caps:
        from_caps = sorted({t for (cap_lid, t) in caps if cap_lid == lid})
        if from_caps:
            return from_caps
    # Promoted HF→lab / scenario sites may be missing from baseline caps.
    if state.test_volumes_by_type:
        from_vol = sorted(state.test_volumes_by_type.get(lid, {}).keys())
        if from_vol:
            return from_vol
    # Greenfield CREATE lab: device present but no typed demand row yet.
    params = state.device_test_parameters_by_type or {}
    devices = [
        d
        for d in state.devices.values()
        if d.facility_id == lid and is_device_operational_for_costing(d)
    ]
    if devices and params:
        union: Set[str] = set()
        for dev in devices:
            union |= supported_tests_for_device_type(str(dev.device_type_id or ""), params)
        if len(union) == 1:
            return sorted(union)
    if not caps and not state.test_volumes_by_type:
        return [ALL_TEST_BUCKET]
    return []


def _typed_route_tests_by_pair(state: NetworkState) -> Dict[Tuple[str, str], set]:
    """(origin_facility_id, dest_lab_id) -> test_type_ids with explicit typed routes."""
    by_pair: Dict[Tuple[str, str], set] = {}
    for route in state.transport_routes or []:
        origin = str(route.origin_facility_id)
        dest = str(route.dest_facility_id)
        tid = getattr(route, "test_type_id", None)
        if tid is None:
            continue
        key = (origin, dest)
        by_pair.setdefault(key, set()).add(str(tid))
    return by_pair


def lab_referred_demand_by_test(state: NetworkState, lab_id: str) -> Dict[str, int]:
    """Per-test demand referred into lab (excludes lab base demand)."""
    if is_inferred_baseline_state(state):
        # Historical totals handled at lab-total layer; avoid double count with onsite HF.
        return {}

    out: Dict[str, int] = {}
    if state.test_volumes_by_type:
        routes_by_pair = _typed_route_tests_by_pair(state)
        for ref in state.referrals:
            if ref.lab_id != lab_id:
                continue
            fac_id = str(ref.facility_id)
            if fac_id == lab_id:
                continue
            pair = (fac_id, str(lab_id))
            typed_tests = routes_by_pair.get(pair)
            has_typed_routes = typed_tests is not None and len(typed_tests) > 0

            if has_typed_routes:
                for tid in typed_tests:
                    vol = route_sample_volume(state, fac_id, str(lab_id), test_type_id=tid)
                    if vol is not None:
                        out[tid] = out.get(tid, 0) + vol
                    else:
                        v = int((state.test_volumes_by_type.get(fac_id) or {}).get(tid, 0) or 0)
                        if v > 0:
                            out[tid] = out.get(tid, 0) + v
            else:
                for test_id, vol in (state.test_volumes_by_type.get(fac_id) or {}).items():
                    tid = str(test_id)
                    v = int(vol or 0)
                    if v <= 0:
                        continue
                    out[tid] = out.get(tid, 0) + v
        return out

    # Single-test aggregate fallback
    total = 0
    for ref in state.referrals:
        if ref.lab_id != lab_id or ref.facility_id == lab_id:
            continue
        fac_id = str(ref.facility_id)
        vol = origin_route_sample_volume_total(state, fac_id, str(lab_id))
        if vol is not None:
            total += vol
        else:
            total += int(state.test_volumes.get(ref.facility_id, 0) or 0)
    if total:
        out[ALL_TEST_BUCKET] = total
    return out


def lab_total_demand_by_test(state: NetworkState, lab_id: str) -> Dict[str, int]:
    """Base + referred per-test demand processed at a lab."""
    if is_inferred_baseline_state(state):
        return dict(historical_lab_demand_by_test(state, lab_id))

    out = dict(lab_referred_demand_by_test(state, lab_id))
    if state.test_volumes_by_type:
        for test_id, vol in (state.test_volumes_by_type.get(lab_id) or {}).items():
            out[str(test_id)] = out.get(str(test_id), 0) + int(vol or 0)
        return out
    # lab_referred_demand_by_test already sums per-edge route volumes; add onsite lab demand only
    base = int(state.test_volumes.get(lab_id, 0) or 0)
    if base:
        out[ALL_TEST_BUCKET] = out.get(ALL_TEST_BUCKET, 0) + base
    return out


def per_test_capacity_at_lab(
    state: NetworkState,
    lab_id: str,
    working_days: int,
) -> Dict[str, float]:
    """
    Annual test capacity C_{l,t} from active capable devices (sum of mtps * B_d per test).
    """
    params = state.device_test_parameters_by_type or {}
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    out: Dict[str, float] = {}

    if not state.test_volumes_by_type and not (state.facility_test_capabilities or params):
        # Legacy single-test: sum operational device annual_capacity
        cap = sum(
            d.annual_capacity(working_days)
            for d in devices
            if is_device_operational_for_costing(d)
        )
        if cap:
            out[ALL_TEST_BUCKET] = float(cap)
        return out

    active = set(active_tests_at_lab(state, lab_id))

    for dev in devices:
        if not is_device_operational_for_costing(dev):
            continue
        budget = device_shift_budget(dev, working_days)
        if budget <= 0:
            continue
        dt_id = str(dev.device_type_id or "")
        running = tests_currently_running_on_device(state, dev.device_id)
        if not running:
            active_lab = set(active_tests_at_lab(state, lab_id))
            if active_lab:
                # Historical projects: no device history yet but lab has active test signal.
                supported = supported_tests_for_device_type(dt_id, params)
                running = supported & active_lab
            elif _legacy_activity_fallback(state):
                supported = supported_tests_for_device_type(dt_id, params)
                if not supported and dev.max_tests_per_shift > 0:
                    supported = {ALL_TEST_BUCKET}
                running = supported
        for test_id in running:
            if test_id != ALL_TEST_BUCKET and test_id not in active:
                continue
            if test_id == ALL_TEST_BUCKET:
                mtps = dev.max_tests_per_shift
            else:
                mtps = mtps_for_device_type(dt_id, test_id, params)
            if mtps <= 0:
                continue
            out[test_id] = out.get(test_id, 0.0) + float(mtps) * budget
    return out


def _capable_devices_for_test(
    state: NetworkState,
    lab_id: str,
    test_type_id: str,
) -> List[DeviceInventory]:
    """Catalogue-capable devices at a lab for a test (integration eligibility / gap checks)."""
    params = state.device_test_parameters_by_type or {}
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    eligible: List[DeviceInventory] = []
    for dev in devices:
        if not is_device_operational_for_costing(dev):
            continue
        dt_id = str(dev.device_type_id or "")
        if test_type_id == ALL_TEST_BUCKET:
            if dev.max_tests_per_shift > 0:
                eligible.append(dev)
            continue
        supported = supported_tests_for_device_type(dt_id, params)
        running = tests_currently_running_on_device(state, dev.device_id)
        if test_type_id in supported or test_type_id in running:
            eligible.append(dev)
    return eligible


def _running_devices_for_test(
    state: NetworkState,
    lab_id: str,
    test_type_id: str,
) -> List[DeviceInventory]:
    """Devices currently processing a test — used for ρ* packing and capacity."""
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    eligible: List[DeviceInventory] = []
    for dev in devices:
        if not is_device_operational_for_costing(dev):
            continue
        running = tests_currently_running_on_device(state, dev.device_id)
        if test_type_id == ALL_TEST_BUCKET:
            if ALL_TEST_BUCKET in running or (
                dev.max_tests_per_shift > 0 and _legacy_activity_fallback(state)
            ):
                eligible.append(dev)
            continue
        if test_type_id in running:
            eligible.append(dev)
    return eligible


def lab_shift_packing_result(
    state: NetworkState,
    lab_id: str,
    working_days: int,
    demand_by_test: Optional[Dict[str, int]] = None,
) -> LabShiftPackingResult:
    """
    Greedy dedicated-first shift packing; exposes ρ* and residual over-budget tests.

    ``demand_by_test`` is an optional precomputed demand map (pure function of state) so
    callers that already computed it can avoid a redundant scan. Result is identical.
    """
    if demand_by_test is None:
        demand_by_test = lab_total_demand_by_test(state, lab_id)
    if not demand_by_test:
        return LabShiftPackingResult(0.0, {}, {})

    if ALL_TEST_BUCKET in demand_by_test and len(demand_by_test) == 1:
        cap = sum(
            d.annual_capacity(working_days)
            for d in state.devices.values()
            if d.facility_id == lab_id
        )
        vol = float(demand_by_test[ALL_TEST_BUCKET])
        if cap <= 0:
            rho = CAPABILITY_GAP_UTIL if vol > 0 else 0.0
            residual = {ALL_TEST_BUCKET: vol} if vol > 0 and cap <= 0 else {}
            return LabShiftPackingResult(
                rho, {}, residual, capability_gap=vol > 0 and cap <= 0
            )
        rho = vol / float(cap)
        residual = (
            {ALL_TEST_BUCKET: max(0.0, vol - cap)}
            if rho > 1.0 + 1e-6
            else {}
        )
        return LabShiftPackingResult(rho, {}, residual)

    params = state.device_test_parameters_by_type or {}
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    if not devices:
        return LabShiftPackingResult(
            CAPABILITY_GAP_UTIL,
            {},
            {str(t): float(v) for t, v in demand_by_test.items() if float(v) > 0},
            capability_gap=True,
        )

    for test_id, vol in demand_by_test.items():
        if int(vol) <= 0:
            continue
        if not _capable_devices_for_test(state, lab_id, test_id):
            return LabShiftPackingResult(
                CAPABILITY_GAP_UTIL,
                {},
                {str(t): float(v) for t, v in demand_by_test.items() if float(v) > 0},
                capability_gap=True,
            )

    tests = sorted(
        [t for t, v in demand_by_test.items() if int(v) > 0],
        key=lambda t: len(_running_devices_for_test(state, lab_id, t)),
    )

    device_load: Dict[str, float] = {d.device_id: 0.0 for d in devices}
    remaining = {t: float(demand_by_test[t]) for t in tests}
    residual: Dict[str, float] = {}
    allocated_by_device_test: Dict[Tuple[str, str], float] = {}

    for test_id in tests:
        need = remaining.get(test_id, 0.0)
        if need <= 0:
            continue
        candidates = _running_devices_for_test(state, lab_id, test_id)
        if not candidates:
            candidates = _capable_devices_for_test(state, lab_id, test_id)

        def _dedicated_score(dev: DeviceInventory) -> int:
            dt_id = str(dev.device_type_id or "")
            sup = supported_tests_for_device_type(dt_id, params)
            if test_id == ALL_TEST_BUCKET:
                return 0
            if len(sup) <= 1:
                return 0
            return 1

        candidates.sort(key=_dedicated_score)
        for dev in candidates:
            if need <= 0:
                break
            dt_id = str(dev.device_type_id or "")
            if test_id == ALL_TEST_BUCKET:
                mtps = dev.max_tests_per_shift
            else:
                mtps = mtps_for_device_type(dt_id, test_id, params)
            if mtps <= 0:
                continue
            budget = device_shift_budget(dev, working_days)
            spare_units = max(0.0, budget - device_load[dev.device_id])
            max_tests = spare_units * float(mtps)
            take = min(need, max_tests)
            device_load[dev.device_id] += take / float(mtps)
            if take > 1e-6:
                key = (dev.device_id, str(test_id))
                allocated_by_device_test[key] = (
                    allocated_by_device_test.get(key, 0.0) + take
                )
            need -= take
        if need > 1e-6:
            residual[str(test_id)] = need
            overflow_candidates = sorted(
                candidates,
                key=lambda d: (device_load[d.device_id], str(d.device_id)),
            )
            for dev in overflow_candidates:
                if need <= 1e-6:
                    break
                dt_id = str(dev.device_type_id or "")
                if test_id == ALL_TEST_BUCKET:
                    mtps = dev.max_tests_per_shift
                else:
                    mtps = mtps_for_device_type(dt_id, test_id, params)
                if mtps <= 0:
                    continue
                device_load[dev.device_id] += need / float(mtps)
                key = (dev.device_id, str(test_id))
                allocated_by_device_test[key] = (
                    allocated_by_device_test.get(key, 0.0) + need
                )
                need = 0.0

    rho = 0.0
    for dev in devices:
        budget = device_shift_budget(dev, working_days)
        if budget <= 0:
            continue
        rho = max(rho, device_load[dev.device_id] / budget)
    return LabShiftPackingResult(
        rho, device_load, residual, allocated_by_device_test=allocated_by_device_test
    )


def rho_star_lab_utilization(
    state: NetworkState,
    lab_id: str,
    working_days: int,
    demand_by_test: Optional[Dict[str, int]] = None,
) -> float:
    """
    Minimum per-device shift fill factor ρ* that packs all per-test demand at the lab.
    """
    return lab_shift_packing_result(
        state, lab_id, working_days, demand_by_test=demand_by_test
    ).rho


def device_rho_at_lab(
    state: NetworkState,
    lab_id: str,
    working_days: int,
) -> Dict[str, float]:
    """Per-device shift fill factor ρ from greedy packing at a lab."""
    packing = lab_shift_packing_result(state, lab_id, working_days)
    out: Dict[str, float] = {}
    for dev in state.devices.values():
        if dev.facility_id != lab_id:
            continue
        budget = device_shift_budget(dev, working_days)
        load = packing.device_load.get(dev.device_id, 0.0)
        out[dev.device_id] = (load / budget) if budget > 0 else 0.0
    return out


def _best_mtps_at_lab(
    state: NetworkState,
    lab_id: str,
    working_days: int,
) -> float:
    """Highest tests-per-shift among running/capable devices at a lab."""
    params = state.device_test_parameters_by_type or {}
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    best = 1.0
    for dev in devices:
        running = tests_currently_running_on_device(state, dev.device_id)
        dt_id = str(dev.device_type_id or "")
        if not running:
            running = supported_tests_for_device_type(dt_id, params) or {ALL_TEST_BUCKET}
        for test_id in running:
            if test_id == ALL_TEST_BUCKET:
                mtps = float(dev.max_tests_per_shift or 0)
            else:
                mtps = float(mtps_for_device_type(dt_id, test_id, params) or 0)
            if mtps > best:
                best = mtps
    return max(1.0, best)


def _all_devices_at_max_shifts(state: NetworkState, lab_id: str) -> bool:
    """True when every device at the lab is already at its configured max shifts."""
    devices = [d for d in state.devices.values() if d.facility_id == lab_id]
    if not devices:
        return False
    for dev in devices:
        meta_max = (dev.metadata or {}).get("max_shifts_limit")
        cap = int(meta_max) if meta_max is not None else int(dev.max_shifts or 1)
        if int(dev.max_shifts or 1) < cap:
            return False
    return True


def residual_test_gaps_for_lab(
    state: NetworkState,
    lab_id: str,
    working_days: int,
    threshold_high: float = 1.0,
) -> Dict[str, float]:
    """
    Residual test-volume gaps for device expansion at a lab.

    Priority: packing residual (incl. ALL bucket) → per-test overflow → legacy
    aggregate bucket → shift-equivalent scalar converted via best mtps.
    """
    packing = lab_shift_packing_result(state, lab_id, working_days)
    gaps: Dict[str, float] = {
        str(t): float(v)
        for t, v in packing.residual_over_budget_by_test.items()
        if float(v) > 1e-6
    }
    if gaps:
        return gaps

    demand_by_test = lab_total_demand_by_test(state, lab_id)
    cap_by_test = per_test_capacity_at_lab(state, lab_id, working_days)
    for test_id, vol in demand_by_test.items():
        v = float(vol or 0)
        if v <= 0:
            continue
        c = float(cap_by_test.get(test_id, 0.0))
        # Match expansion gap math: relief when demand exceeds target util × capacity
        th = max(threshold_high, 1e-6)
        gap = max(0.0, v / th - c)
        if gap > 1e-6:
            gaps[str(test_id)] = gap
    if gaps:
        return gaps

    rho = packing.rho
    if (
        rho > threshold_high + 1e-6
        and ALL_TEST_BUCKET in demand_by_test
        and len(demand_by_test) == 1
    ):
        total_cap = sum(
            d.annual_capacity(working_days)
            for d in state.devices.values()
            if d.facility_id == lab_id
        )
        vol = float(demand_by_test[ALL_TEST_BUCKET])
        th = max(threshold_high, 1e-6)
        gap = max(0.0, vol / th - float(total_cap))
        if gap > 1e-6:
            return {ALL_TEST_BUCKET: gap}

    seg = shift_equivalent_gap(
        state, lab_id, working_days, target_util=threshold_high
    )
    if seg > 1e-6 and (
        _all_devices_at_max_shifts(state, lab_id) or rho > threshold_high + 1e-6
    ):
        best_mtps = _best_mtps_at_lab(state, lab_id, working_days)
        overflow = overflow_tests_at_lab(
            state, lab_id, working_days, threshold_high=threshold_high
        )
        if overflow:
            for test_id in overflow:
                dt_mtps = best_mtps
                if test_id != ALL_TEST_BUCKET:
                    params = state.device_test_parameters_by_type or {}
                    devices = [
                        d for d in state.devices.values() if d.facility_id == lab_id
                    ]
                    for dev in devices:
                        dt_id = str(dev.device_type_id or "")
                        m = float(mtps_for_device_type(dt_id, test_id, params) or 0)
                        if m > 0:
                            dt_mtps = m
                            break
                gaps[str(test_id)] = seg * dt_mtps
        else:
            gaps[ALL_TEST_BUCKET] = seg * best_mtps
    return gaps


def lab_shift_demand(state: NetworkState, lab_id: str, working_days: int) -> float:
    """Total shift-units of demand at a lab (sum_t vol_t / mtps_{best})."""
    demand_by_test = lab_total_demand_by_test(state, lab_id)
    params = state.device_test_parameters_by_type or {}
    total = 0.0
    for test_id, vol in demand_by_test.items():
        v = float(vol or 0)
        if v <= 0:
            continue
        if test_id == ALL_TEST_BUCKET:
            # Use slowest device mtps at lab as conservative shift load
            devices = [d for d in state.devices.values() if d.facility_id == lab_id]
            if not devices:
                return float("inf")
            mtps = max(1, min(d.max_tests_per_shift for d in devices))
            total += v / float(mtps)
            continue
        devices = _running_devices_for_test(state, lab_id, test_id)
        if not devices:
            devices = _capable_devices_for_test(state, lab_id, test_id)
        if not devices:
            return float("inf")
        best_mtps = max(
            mtps_for_device_type(str(d.device_type_id or ""), test_id, params) or d.max_tests_per_shift
            for d in devices
        )
        total += v / float(max(1, best_mtps))
    return total


def lab_shift_budget_total(state: NetworkState, lab_id: str, working_days: int) -> float:
    """Sum of device shift budgets at a lab."""
    return sum(
        device_shift_budget(d, working_days)
        for d in state.devices.values()
        if d.facility_id == lab_id
    )


def shift_equivalent_gap(
    state: NetworkState,
    lab_id: str,
    working_days: int,
    target_util: float = 1.0,
) -> float:
    """Scalar shift-unit shortfall G_lab (positive = need more capacity)."""
    demand = lab_shift_demand(state, lab_id, working_days)
    budget = lab_shift_budget_total(state, lab_id, working_days) * target_util
    return max(0.0, demand - budget)


def overflow_tests_at_lab(
    state: NetworkState,
    lab_id: str,
    working_days: int,
    threshold_high: float = 1.0,
) -> List[str]:
    """Tests needing relief vs configurable high utilization threshold."""
    demand = lab_total_demand_by_test(state, lab_id)
    cap = per_test_capacity_at_lab(state, lab_id, working_days)
    out: List[str] = []
    th = max(threshold_high, 1e-6)
    for test_id, vol in demand.items():
        c = float(cap.get(test_id, 0.0))
        v = float(vol or 0)
        target_cap = v / th
        gap = max(0.0, target_cap - c)
        if gap > 1e-6:
            out.append(test_id)
        elif c <= 0 and int(vol) > 0:
            out.append(test_id)
    return out


def integration_candidates_for_test(
    state: NetworkState,
    lab_id: str,
    test_type_id: str,
    working_days: int,
) -> List[Tuple[DeviceInventory, float]]:
    """
    Multi-test device already running another assay; catalogue supports T but not yet running it.
    Trial simulation in the ladder gates actual relief.
    """
    params = state.device_test_parameters_by_type or {}
    results: List[Tuple[DeviceInventory, float]] = []
    for dev in state.devices.values():
        if dev.facility_id != lab_id:
            continue
        dt_id = str(dev.device_type_id or "")
        supported = supported_tests_for_device_type(dt_id, params)
        running = tests_currently_running_on_device(state, dev.device_id)
        if not is_device_operational_for_costing(dev):
            continue
        if not running or len(supported) <= 1:
            continue
        if test_type_id == ALL_TEST_BUCKET:
            if dev.max_tests_per_shift <= 0:
                continue
        elif test_type_id not in supported:
            continue
        if test_type_id in running and test_type_id != ALL_TEST_BUCKET:
            continue
        # Full shift budget for trial — do not pre-filter by equal-split spare estimate
        budget = device_shift_budget(dev, working_days)
        if budget > 1e-6:
            results.append((dev, budget))
    return results


def activate_test_on_device(state: NetworkState, device_id: str, test_type_id: str) -> None:
    """Record that a device is running an integrated test (in-memory scenario state)."""
    if state.device_active_tests is None:
        state.device_active_tests = {}
    active = set(state.device_active_tests.get(device_id, set()))
    active.add(str(test_type_id))
    state.device_active_tests[device_id] = active


def union_lab_capability(state: NetworkState, lab_id: str, test_type_id: str) -> None:
    """Activate test at lab in scenario capability set."""
    if state.facility_test_capabilities is None:
        state.facility_test_capabilities = set()
    state.facility_test_capabilities.add((str(lab_id), str(test_type_id)))


def rescale_device_shifts(
    dev: Any,
    new_shifts: int,
    working_days: int,
    old_shifts: Optional[int] = None,
) -> None:
    """
    Single source of truth for an ADD_SHIFT / manual-shift edit on a device.

    Updates ``max_shifts`` only; ``annual_capacity()`` derives capacity live from mps.
    """
    _ = working_days, old_shifts  # kept for call-site compatibility
    dev.max_shifts = max(1, int(new_shifts))


def is_multi_test_project(state: NetworkState) -> bool:
    """True when per-test demand or params exist beyond a single aggregate bucket."""
    if state.test_volumes_by_type:
        tests: Set[str] = set()
        for by_test in state.test_volumes_by_type.values():
            tests.update(by_test.keys())
        return len(tests) > 1
    if state.device_test_parameters_by_type:
        tests = {k[1] for k in state.device_test_parameters_by_type.keys()}
        return len(tests) > 1
    return False


def lab_has_mixed_test_devices(state: NetworkState, lab_id: str) -> bool:
    """True if lab has devices supporting different test menus (MILP y-eligible)."""
    params = state.device_test_parameters_by_type or {}
    menus: Set[FrozenSet[str]] = set()
    for dev in state.devices.values():
        if dev.facility_id != lab_id:
            continue
        sup = supported_tests_for_device_type(str(dev.device_type_id or ""), params)
        if len(sup) > 1:
            return True
        menus.add(frozenset(sup))
    return len(menus) > 1
