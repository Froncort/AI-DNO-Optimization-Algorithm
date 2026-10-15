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
Cost calculator with standardized cost definitions.

Test-volume allocation helpers (``allocated_tests_for_device``, ``allocation_batch``,
``device_testing_cost_annual_float``) are the single source of truth for splitting lab
demand across devices for utilization and costing. Multi-device labs use cheapest-CPT
greedy fill; testing ₹ is assigned tests × that device's ``cost_per_test``.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, MutableMapping, Optional, Tuple
from collections import defaultdict
from decimal import Decimal

from dno_core.models.device import DeviceInventory
from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.referral import resolve_sample_transport_cpk
from dno_core.domain.transport_referral_utils import (
    _is_onsite_referral,
    _referral_distance,
    _route_metadata_dict,
    origin_route_sample_volume_total,
    parse_route_annual_samples,
    route_sample_volume,
)


@dataclass(frozen=True)
class SampleTransportLeg:
    """
    One billable primary referral transport leg.

    All dashboard / export transport ₹ breakdowns (card, Admin Area, by-mode, lab-wise)
    group rows from ``CostCalculator.iter_sample_transport_legs`` — never re-implement
    distance × cpk × volume locally.

    ``dist_bill`` uses stored ``referral.distance_km × circuity`` only (no geodesic).
    Display surfaces (Routes ``distance_km``, admin ASD) may still use geodesic fallback.
    """

    origin_fid: str
    lab_id: str
    cost: float  # unrounded annual ₹ for this leg (dist_bill × cpk × volume)
    volume: int  # annual sample volume after MaxFreq×W vs demand rule
    dist_bill: float  # stored referral distance × circuity (road-equivalent km)
    cpk: float  # cost per km from matched route or EngineConfig fallback


# ──────────────────────────────────────────────────────────────────────────────
# Lab demand → device test allocation & testing cost (single source of truth)
# ──────────────────────────────────────────────────────────────────────────────


def is_device_operational_for_costing(device: Any) -> bool:
    """Only OPERATIONAL devices receive test allocation and cost attribution."""
    st = (getattr(device, "status", None) or "OPERATIONAL").strip().upper()
    return st == "OPERATIONAL"


# Sentinel when per-test historical rows are unavailable (matches shift_capacity.ALL_TEST_BUCKET).
_ALL_TEST_BUCKET = "ALL"

# Re-export route-volume helpers for callers that import from cost (e.g. shift_capacity).
__all__ = [
    "parse_route_annual_samples",
    "route_sample_volume",
    "origin_route_sample_volume_total",
]


def historical_lab_demand_by_test(state: NetworkState, lab_id: str) -> Dict[str, int]:
    """Per-test observed lab workload from Historical Testing."""
    ls = str(lab_id)
    out: Dict[str, int] = {}
    dtv_by_test = state.device_type_test_volumes
    if dtv_by_test:
        for (fid, _dtid, ttid), vol in dtv_by_test.items():
            if str(fid) != ls:
                continue
            v = int(vol or 0)
            if v <= 0:
                continue
            tid = str(ttid)
            out[tid] = out.get(tid, 0) + v
        return out
    dtv = state.device_type_volumes
    if not dtv:
        return out
    total = sum(int(v or 0) for (fid, _dt), v in dtv.items() if str(fid) == ls)
    if total > 0:
        out[_ALL_TEST_BUCKET] = total
    return out


def historical_lab_demand_total(state: NetworkState, lab_id: str) -> int:
    """Total observed lab workload from Historical Testing at one facility."""
    by_test = historical_lab_demand_by_test(state, lab_id)
    if by_test:
        return sum(by_test.values())
    ls = str(lab_id)
    dtv = state.device_type_volumes or {}
    return sum(int(v or 0) for (fid, _dt), v in dtv.items() if str(fid) == ls)


def _has_bulk_upload_routes(state: NetworkState) -> bool:
    for route in state.transport_routes or []:
        meta = _route_metadata_dict(route)
        if str(meta.get("source") or "").strip().lower() == "bulk_upload":
            return True
    return False


def _has_synthetic_referrals(state: NetworkState) -> bool:
    for ref in state.referrals or []:
        if getattr(ref, "is_synthetic", False):
            return True
    return False


def is_inferred_baseline_state(state: NetworkState) -> bool:
    # Always False — inferred-baseline cost path is unused.
    return False


def _legacy_lab_total_demand(lab_id: Any, state: NetworkState) -> int:
    """Referred + onsite HF when no historical baseline applies."""
    ls = str(lab_id)
    referred = sum(
        int(state.test_volumes.get(r.facility_id, 0) or 0)
        for r in state.referrals
        if str(r.lab_id) == ls and str(r.facility_id) != ls
    )
    base = int(state.test_volumes.get(lab_id, 0) or state.test_volumes.get(ls, 0) or 0)
    return referred + base


def _explicit_lab_processed_demand(state: NetworkState, lab_id: str) -> int:
    """Route sample counts plus onsite HF at lab (explicit referral projects)."""
    ls = str(lab_id)
    inbound = 0
    seen_pairs: set = set()
    for ref in state.referrals or []:
        if str(ref.lab_id) != ls or str(ref.facility_id) == ls:
            continue
        pair = (str(ref.facility_id), ls)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        vol = origin_route_sample_volume_total(state, pair[0], ls)
        if vol is not None:
            inbound += vol
        else:
            inbound += int(state.test_volumes.get(ref.facility_id, 0) or 0)
    onsite = int(state.test_volumes.get(lab_id, 0) or state.test_volumes.get(ls, 0) or 0)
    return inbound + onsite


def _origin_explicit_route_volume_total(state: NetworkState, origin_id: str) -> Optional[int]:
    """Sum explicit per-edge route samples for one HF across all active outbound lanes."""
    oid = str(origin_id)
    total = 0
    found = False
    seen_dest: set = set()
    for ref in state.referrals or []:
        if str(ref.facility_id) != oid or str(ref.lab_id) == oid:
            continue
        dest = str(ref.lab_id)
        if dest in seen_dest:
            continue
        seen_dest.add(dest)
        vol = origin_route_sample_volume_total(state, oid, dest)
        if vol is not None:
            total += int(vol)
            found = True
    return total if found else None


def _scenario_dynamic_lab_processed_demand(state: NetworkState, lab_id: str) -> int:
    """
    Scenario lab demand: current referrals + onsite, preserving multi-lab per-edge splits
    when baseline route metadata exists; full origin demand for new/redesigned lanes.
    """
    ls = str(lab_id)
    inbound = 0
    seen_pairs: set = set()
    for ref in state.referrals or []:
        if str(ref.lab_id) != ls or str(ref.facility_id) == ls:
            continue
        origin = str(ref.facility_id)
        pair = (origin, ls)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        current_origin = int(state.test_volumes.get(origin, 0) or 0)
        edge_vol = origin_route_sample_volume_total(state, origin, ls)
        if edge_vol is not None:
            origin_total = _origin_explicit_route_volume_total(state, origin)
            if origin_total and origin_total > 0:
                inbound += int(round(current_origin * (edge_vol / origin_total)))
            else:
                inbound += int(edge_vol)
        else:
            inbound += current_origin
    onsite = int(state.test_volumes.get(lab_id, 0) or state.test_volumes.get(ls, 0) or 0)
    return inbound + onsite


def baseline_lab_processed_demand(state: NetworkState, lab_id: Any) -> int:
    """Authoritative annual tests processed at a lab for utilization/costing."""
    if not getattr(state, "use_historical_lab_baseline", True):
        return _scenario_dynamic_lab_processed_demand(state, str(lab_id))
    ls = str(lab_id)
    if _has_bulk_upload_routes(state):
        return _explicit_lab_processed_demand(state, ls)
    return _legacy_lab_total_demand(lab_id, state)


def transport_volume_for_origin_leg(
    state: NetworkState,
    origin_id: str,
    lab_id: str,
    *,
    max_freq: int,
    hf_fallback: int,
    hf_volume_fn: Any,
) -> int:
    """
    Billable sample volume for one origin→lab leg.

    Uses route metadata when present (no HF MaxFreq cap); otherwise HF demand rule.
    """
    route_total = origin_route_sample_volume_total(state, origin_id, lab_id)
    if route_total is not None:
        return int(route_total)
    return int(hf_volume_fn(hf_fallback, max_freq))


def lab_total_demand(lab_id: Any, state: NetworkState) -> int:
    """Annual tests processed at lab: historical baseline, explicit routes, or legacy HF."""
    return baseline_lab_processed_demand(state, lab_id)


def min_lab_cost_per_test(lab_devices: List[DeviceInventory]) -> Decimal:
    """Minimum ``cost_per_test`` among devices at a lab."""
    if not lab_devices:
        return Decimal("0")
    return min((d.cost_per_test for d in lab_devices), default=Decimal("0"))


def _split_int_among(weights: List[int], total: int) -> List[int]:
    """Allocate ``total`` across len(weights) slots proportionally using largest remainder."""
    n = len(weights)
    total = max(0, int(total))
    if n == 0:
        return []
    if total == 0:
        return [0] * n
    ws = sum(max(0, int(w)) for w in weights)
    if ws <= 0:
        return [total // n + (1 if i < total % n else 0) for i in range(n)]
    floors: List[int] = []
    fracs: List[float] = []
    for w in weights:
        w = max(0, int(w))
        exact = total * (w / ws)
        fl = int(exact // 1)
        floors.append(fl)
        fracs.append(exact - fl)
    s = sum(floors)
    remainder = total - s
    order = sorted(range(n), key=lambda i: (-fracs[i], -weights[i]))
    out = floors[:]
    for i in order[:remainder]:
        out[i] += 1
    return out


def _greedy_fill_by_cost(
    devices: List[DeviceInventory],
    total: int,
    working_days: int,
    used: Optional[Mapping[str, int]] = None,
) -> Dict[str, int]:
    """Fill cheapest devices first up to annual capacity; overflow splits proportionally."""
    used_map = {str(k): int(v or 0) for k, v in (used or {}).items()}
    out = {str(d.device_id): 0 for d in devices}
    remaining = max(0, int(total))
    if remaining <= 0 or not devices:
        return out
    ordered = sorted(
        devices,
        key=lambda d: (Decimal(str(d.cost_per_test or 0)), str(d.device_id)),
    )
    for d in ordered:
        did = str(d.device_id)
        avail = max(0, int(d.annual_capacity(working_days)) - used_map.get(did, 0))
        take = min(avail, remaining)
        out[did] += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        overflow = _split_int_among(
            [d.annual_capacity(working_days) for d in ordered],
            remaining,
        )
        for d, amt in zip(ordered, overflow):
            out[str(d.device_id)] += amt
    return out


def _allocation_for_lab_devices(
    lab_id: Any,
    lab_devices: List[DeviceInventory],
    lab_volume: int,
    working_days: int,
    dtv_raw: Mapping[Tuple[str, str], int],
) -> Tuple[MutableMapping[str, int], MutableMapping[str, int]]:
    """Return ``hist_tests`` and ``residual_tests`` per ``device.device_id``.

    All processed lab demand is assigned cheapest-device-first via ``_greedy_fill_by_cost``.
    ``hist_tests`` are zero; ``residual_tests`` hold the greedy allocation (compat tuple shape).
    """
    _ = (lab_id, dtv_raw)
    lab_volume = max(0, int(lab_volume))
    devices_sorted = sorted(
        lab_devices, key=lambda d: (str(d.device_id), str(getattr(d, "device_type", "")))
    )
    if not devices_sorted:
        return {}, {}
    total_cap_lab = sum(d.annual_capacity(working_days) for d in devices_sorted)
    if lab_volume <= 0 or total_cap_lab <= 0:
        zeros = {str(d.device_id): 0 for d in devices_sorted}
        return dict(zeros), dict(zeros)

    greedy = _greedy_fill_by_cost(devices_sorted, lab_volume, working_days)
    hist_tests: MutableMapping[str, int] = {str(d.device_id): 0 for d in devices_sorted}
    res_tests: MutableMapping[str, int] = {
        did: int(amt) for did, amt in greedy.items()
    }
    return hist_tests, res_tests


def allocation_batch(
    state: NetworkState, working_days: int
) -> Tuple[MutableMapping[str, int], MutableMapping[str, int], MutableMapping[str, int]]:
    """Per ``device_id``: ``allocated_tests``, ``hist_tests`` (0), ``residual_tests`` (greedy assign)."""
    dtv_raw = getattr(state, "device_type_volumes", None) or {}

    labs: MutableMapping[Any, List[DeviceInventory]] = defaultdict(list)
    for d in state.devices.values():
        if is_device_operational_for_costing(d):
            labs[d.facility_id].append(d)

    alloc: MutableMapping[str, int] = {}
    hmap: MutableMapping[str, int] = {}
    rmap: MutableMapping[str, int] = {}

    for lab_id, lab_devices in labs.items():
        vol = lab_total_demand(lab_id, state)
        ht, rt = _allocation_for_lab_devices(lab_id, lab_devices, vol, working_days, dtv_raw)
        all_ids = {str(dd.device_id) for dd in lab_devices}
        for did in all_ids:
            h = ht.get(did, 0)
            r = rt.get(did, 0)
            hmap[did] = int(h)
            rmap[did] = int(r)
            alloc[did] = int(h + r)
    return alloc, hmap, rmap


def allocated_tests_for_device(
    device: Any,
    state: NetworkState,
    working_days: int,
    *,
    allocation_bundle: Optional[Tuple[MutableMapping[str, int], MutableMapping[str, int], MutableMapping[str, int]]] = None,
) -> int:
    """Annual tests attributed to ``device`` for costing and utilization (greedy allocation)."""
    if allocation_bundle is None:
        alloc, _, _ = allocation_batch(state, working_days)
    else:
        alloc, _, _ = allocation_bundle
    return int(alloc.get(str(device.device_id), 0))


def device_testing_cost_annual_float(
    device: Any,
    state: NetworkState,
    working_days: int,
    hist_tests: Optional[int] = None,
    residual_tests: Optional[int] = None,
    min_lab_cpt: Optional[Decimal] = None,
    *,
    allocation_bundle: Optional[Tuple[MutableMapping[str, int], MutableMapping[str, int], MutableMapping[str, int]]] = None,
) -> float:
    """Annual testing rupees: assigned tests × device ``cost_per_test``."""
    _ = min_lab_cpt
    if not is_device_operational_for_costing(device):
        return 0.0

    did = str(device.device_id)

    if hist_tests is not None and residual_tests is not None:
        assigned = int(hist_tests) + int(residual_tests)
    elif allocation_bundle is None:
        alloc, _, _ = allocation_batch(state, working_days)
        assigned = int(alloc.get(did, 0))
    else:
        alloc, _, _ = allocation_bundle
        assigned = int(alloc.get(did, 0))

    if assigned <= 0:
        return 0.0
    cpt = Decimal(str(device.cost_per_test or 0))
    return float(cpt * assigned)


def network_testing_totals_and_by_lab(
    state: NetworkState,
    working_days: int,
) -> Tuple[Decimal, Dict[str, Decimal]]:
    """Annual testing rupees: network total + per lab host facility id."""
    labs: MutableMapping[Any, List[DeviceInventory]] = defaultdict(list)
    for d in state.devices.values():
        if is_device_operational_for_costing(d):
            labs[d.facility_id].append(d)

    alloc, _, _ = allocation_batch(state, working_days)

    total = Decimal("0")
    by_lab: Dict[str, Decimal] = defaultdict(Decimal)

    for lab_id, lab_devices in labs.items():
        for d in lab_devices:
            did = str(d.device_id)
            n = int(alloc.get(did, 0))
            if n <= 0:
                continue
            amt = Decimal(str(d.cost_per_test or 0)) * n
            total += amt
            by_lab[str(lab_id)] += amt

    return (
        quantize_testing_total(total),
        {lid: quantize_testing_total(val) for lid, val in sorted(by_lab.items(), key=lambda x: x[0])},
    )


def quantize_testing_total(amount: Decimal) -> Decimal:
    """Round accumulated testing ₹ to sensible precision for totals."""
    return amount.quantize(Decimal("0.01"))


def network_catalog_cost_per_test(state: NetworkState, working_days: int) -> float:
    """
    Weighted mean catalog ``cost_per_test`` across operational devices.

    Weights by allocated annual tests when demand exists; otherwise by device capacity.
    """
    bundle = allocation_batch(state, working_days)
    weighted = 0.0
    total_tests = 0
    for d in state.devices.values():
        if not is_device_operational_for_costing(d):
            continue
        n = allocated_tests_for_device(
            d, state, working_days, allocation_bundle=bundle
        )
        cpt = float(d.cost_per_test or 0)
        if n > 0 and cpt > 0:
            weighted += cpt * n
            total_tests += n
    if total_tests > 0:
        return round(weighted / total_tests, 2)

    cap_weighted = 0.0
    cap_total = 0
    for d in state.devices.values():
        if not is_device_operational_for_costing(d):
            continue
        cap = d.annual_capacity(working_days)
        cpt = float(d.cost_per_test or 0)
        if cap > 0 and cpt > 0:
            cap_weighted += cpt * cap
            cap_total += cap
    if cap_total > 0:
        return round(cap_weighted / cap_total, 2)
    return 0.0


class CostCalculator:
    """
    Calculate standardized costs for optimization.
    
    Cost Types:
    - Shift Cost: Yearly HR cost per shift
    - Overhead Cost: Device overhead + warranty (yearly)
    - Startup Cost: One-time new device setup (summed for scenario-added devices; not spread over years)
    - Transport Cost: Relocation cost
    - Testing Cost: Per-test cost
    - Conversion Cost: Facility→Lab upgrade
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize cost calculator.
        
        Args:
            config: Engine configuration with cost parameters
        """
        self.config = config
        self.cost_per_km = float(config.cost_per_km)
        self.fixed_handling_cost = float(config.fixed_handling_cost)
        self.annualization_years = config.annualization_period_years
        self._transport_weeks = int(getattr(config, "transport_model_weeks_per_year", None) or 50)
        _override = getattr(config, "transport_max_freq_per_week_override", None)
        self._freq_override: Optional[int] = int(_override) if _override else None
        # Per-district weekly freq from situational_rules; empty = unused (backward compatible)
        raw_df = (getattr(config, "situational_rules", None) or {}).get(
            "transport_max_freq_per_week_by_admin_area_2"
        ) or {}
        self._district_freq: Dict[str, int] = {}
        if isinstance(raw_df, dict):
            for k, v in raw_df.items():
                name = str(k or "").strip()
                if not name:
                    continue
                try:
                    fv = int(v)
                except (TypeError, ValueError):
                    continue
                if 1 <= fv <= 365:
                    self._district_freq[name.casefold()] = fv
        # Referral / route ``distance_km`` is geodesic (no circuity). One ×1.4 here
        # matches road-style costing and OptiDx-style totals; MILP still optimizes on
        # effective km via ``DistanceCalculator.facility_distance``.
        self._distance_circuity = 1.4

    def _recost_modes_active(self) -> bool:
        """True when scenario re-costs all legs using selected mode catalog rates."""
        allocs = getattr(self.config, "sample_transport_mode_allocations", None)
        return bool(allocs)

    def _mode_catalog_entry(self, mode_id: str) -> Dict[str, Any]:
        catalog = getattr(self.config, "sample_transport_mode_catalog", None) or {}
        return dict(catalog.get(str(mode_id), {}) or {})

    def _blended_mode_cpk(self) -> float:
        """Weighted mean cost/km from scenario mode allocations and catalog."""
        allocs = getattr(self.config, "sample_transport_mode_allocations", None) or []
        if not allocs:
            return resolve_sample_transport_cpk(None, self.config)
        total = 0.0
        for mode_id, frac in allocs:
            entry = self._mode_catalog_entry(str(mode_id))
            cpk = float(entry.get("cost_per_km") or 0)
            if cpk <= 0:
                cpk = resolve_sample_transport_cpk(None, self.config)
            total += float(frac) * cpk
        return total if total > 0 else resolve_sample_transport_cpk(None, self.config)

    def _route_counts_for_sample_transport(self, route: Any) -> bool:
        """All active routes count toward volume; scenario recost uses catalog cpk."""
        return True
    
    # ──────────────────────────────────────────────────────────────────────────
    # Device Costs
    # ──────────────────────────────────────────────────────────────────────────
    
    def device_operational_cost_annual(self, device: DeviceInventory) -> Decimal:
        """
        Calculate annual operational cost for a device.
        
        Formula: (shift_cost × max_shifts) + overhead_cost
        """
        return device.operational_cost_annual()
    
    def device_annualized_startup_cost(self, device: DeviceInventory) -> Decimal:
        """
        Startup cost attributed to this device for network totals and exports.

        Uses full one-time ``startup_cost`` (not divided by annualization period) so
        totals match catalogue / procurement figures (e.g. two devices at 37,503 → 75,006).
        """
        return device.startup_cost
    
    def device_total_annual_cost(
        self, 
        device: DeviceInventory,
        include_startup: bool = True
    ) -> Decimal:
        """
        Calculate total annual cost for a device.
        
        Formula: operational_cost + annualized_startup_cost
        """
        return device.total_annual_cost(self.annualization_years, include_startup)
    
    # ──────────────────────────────────────────────────────────────────────────
    # Transport Costs
    # ──────────────────────────────────────────────────────────────────────────
    
    def transport_cost(
        self, 
        distance_km: float,
        cross_boundary: bool = False
    ) -> Decimal:
        """
        Calculate transport/relocation cost.
        
        Formula: (distance × cost_per_km) + fixed_handling
        
        If crossing admin boundary, applies penalty multiplier.
        """
        base_cost = Decimal(str(distance_km * self.cost_per_km)) + Decimal(str(self.fixed_handling_cost))
        
        if cross_boundary:
            penalty = float(self.config.cross_boundary_penalty)
            base_cost = base_cost * Decimal(str(penalty))
        
        return base_cost
    
    def relocation_total_cost(
        self,
        distance_km: float,
        setup_cost: Optional[Decimal] = None,
        cross_boundary: bool = False
    ) -> Decimal:
        """
        Calculate total relocation cost (transport + setup).
        
        Args:
            distance_km: Distance to move device
            setup_cost: Optional additional setup cost at destination
            cross_boundary: Whether relocation crosses admin boundary
        """
        transport = self.transport_cost(distance_km, cross_boundary)
        setup = setup_cost or Decimal("0")
        return transport + setup
    
    def calculate_relocation_cost(
        self,
        device: 'DeviceInventory',
        source_lab: 'Facility',
        target_lab: 'Facility'
    ) -> float:
        """
        Calculate relocation cost for a device between two facilities.
        
        This is a convenience method used by the relocation solver.
        
        Args:
            device: Device to relocate
            source_lab: Source facility
            target_lab: Target facility
            
        Returns:
            Relocation cost in INR as a float
        """
        from dno_core.calculators.distance import DistanceCalculator
        
        # Calculate distance
        distance = DistanceCalculator.facility_distance(source_lab, target_lab) or 0.0
        
        # Check if crossing admin boundary
        cross_boundary = not source_lab.is_in_same_admin_area(
            target_lab,
            self.config.admin_boundary_level
        )
        
        # Calculate total relocation cost
        cost = self.relocation_total_cost(
            distance_km=distance,
            setup_cost=None,
            cross_boundary=cross_boundary
        )
        
        return float(cost)
    
    # ──────────────────────────────────────────────────────────────────────────
    # Testing Costs
    # ──────────────────────────────────────────────────────────────────────────
    
    def _min_cost_per_test_among_devices(
        self, lab_device_list: List[DeviceInventory]
    ) -> Decimal:
        """Cheapest reagent CPT among devices at a lab (for fallback when historical split is missing)."""
        return min_lab_cost_per_test(lab_device_list)

    def testing_cost_annual(
        self,
        device: DeviceInventory,
        tests_conducted: int
    ) -> Decimal:
        """
        Calculate annual testing cost for a device.
        
        Formula: cost_per_test × tests_conducted
        """
        return device.cost_per_test * tests_conducted

    def network_testing_cost_annual(
        self,
        devices: Dict[str, DeviceInventory],
        test_volumes: Dict[str, int],
        state: Optional['NetworkState'] = None,
    ) -> Decimal:
        """
        Total annual testing cost from lab demand split across devices
        (:func:`allocation_batch`) at each device's ``cost_per_test``.
        """
        if state is None:
            return Decimal("0")
        wd = int(state.config.working_days_per_year) if state.config else 300
        total, _ = network_testing_totals_and_by_lab(state, wd)
        return total

    def network_testing_cost_annual_by_lab(
        self,
        devices: Dict[str, DeviceInventory],
        test_volumes: Dict[str, int],
        state: Optional[NetworkState] = None,
    ) -> Dict[str, float]:
        """
        Same attribution as :meth:`network_testing_cost_annual`, split per lab
        (device host ``facility_id``).
        """
        if state is None:
            return {}
        wd = int(state.config.working_days_per_year) if state.config else 300
        _, by_lab = network_testing_totals_and_by_lab(state, wd)
        return {lid: round(float(amount), 2) for lid, amount in sorted(by_lab.items())}

    # ──────────────────────────────────────────────────────────────────────────
    # Sample transport (referral logistics)
    # ──────────────────────────────────────────────────────────────────────────
    
    def _max_ship_frequency_per_week(self, state: NetworkState, facility_id: str) -> int:
        """
        Weekly ship frequency for sample transport at an origin facility.

        Precedence: district override → global override → max route frequency → 1.
        """
        fid = str(facility_id)
        if self._district_freq:
            fac = (getattr(state, "facilities", None) or {}).get(fid) or (
                getattr(state, "labs", None) or {}
            ).get(fid)
            if fac is not None:
                a2 = (
                    getattr(fac, "admin_area_2", None)
                    or getattr(fac, "district", None)
                    or ""
                )
                a2 = str(a2).strip()
                if a2:
                    hit = self._district_freq.get(a2.casefold())
                    if hit is not None:
                        return hit
        if self._freq_override is not None:
            return self._freq_override
        routes = getattr(state, "transport_routes", None) or []
        max_f = 0
        for route in routes:
            if not getattr(route, "is_active", True):
                continue
            if str(getattr(route, "origin_facility_id", "") or "") != fid:
                continue
            fq = int(getattr(route, "frequency_per_week", None) or 0)
            max_f = max(max_f, fq)
        return max_f if max_f > 0 else 1

    def _max_origin_route_frequency_per_week(
        self, state: NetworkState, facility_id: str
    ) -> int:
        """
        Max raw ``frequency_per_week`` on active routes from this facility (ignores
        ``transport_max_freq_per_week_override``). Used only for debug hints.
        """
        routes = getattr(state, "transport_routes", None) or []
        fid = str(facility_id)
        max_f = 0
        for route in routes:
            if not getattr(route, "is_active", True):
                continue
            if str(getattr(route, "origin_facility_id", "") or "") != fid:
                continue
            fq = int(getattr(route, "frequency_per_week", None) or 0)
            max_f = max(max_f, fq)
        return max_f if max_f > 0 else 1

    def _hf_sample_transport_volume_detail(
        self, hf_total_demand: int, hf_max_freq_per_week: int
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Same math as ``_hf_sample_transport_volume``; returns trace fields for logging.

        Let W = transport_model_weeks_per_year (default 50).

        IS (HF MaxFreq × W) < HF Total Demand?
        - If **yes**: use HF MaxFreq × W
        - If **no**: use HF Total Demand
        """
        demand = max(0, int(hf_total_demand or 0))
        mf = max(0, int(hf_max_freq_per_week or 0))
        if mf <= 0:
            mf = 1
        w = self._transport_weeks
        trip_cap_volume = mf * w
        branch = "trip_cap" if trip_cap_volume < demand else "demand"
        vol = trip_cap_volume if trip_cap_volume < demand else demand
        detail = {
            "effective_mf": mf,
            "trip_cap_volume": trip_cap_volume,
            "branch": branch,
            "annual_volume": vol,
        }
        return vol, detail

    def _hf_sample_transport_volume(
        self, hf_total_demand: int, hf_max_freq_per_week: int
    ) -> int:
        """
        Volume factor (samples) for one health facility leg, per spec:

        Let W = transport_model_weeks_per_year (default 50).

        IS (HF MaxFreq × W) < HF Total Demand?
        - If **yes**: use HF MaxFreq × W
        - If **no**: use HF Total Demand

        HF Total Demand = annual tests at that facility (aggregated, no test-type split).
        HF MaxFreq = max ship frequency per week across routes from that facility.
        """
        vol, _ = self._hf_sample_transport_volume_detail(
            hf_total_demand, hf_max_freq_per_week
        )
        return vol

    def _sample_transport_economics_for_facility_lab(
        self, state: NetworkState, facility_id: str, lab_id: str
    ) -> Tuple[float, float, int]:
        """
        Per-leg economics: (cost_per_km, fixed_trip_cost, frequency_per_week).

        Prefers an active route whose origin is the facility and destination is the
        assigned lab; otherwise uses any active route from that facility as a
        template (same mode/cost assumptions, but distance comes from the referral).
        If there is no route from the facility, falls back to Modes catalog / config.
        """
        default_fixed = float(self.fixed_handling_cost)
        if self._recost_modes_active():
            return self._blended_mode_cpk(), default_fixed, 1
        routes = getattr(state, "transport_routes", None) or []
        exact: Any = None
        origin_routes: List[Any] = []
        for route in routes:
            if not getattr(route, "is_active", True):
                continue
            if route.origin_facility_id != facility_id:
                continue
            origin_routes.append(route)
            if route.dest_facility_id == lab_id:
                exact = route
                break
        chosen = exact or (origin_routes[0] if origin_routes else None)
        if chosen is not None:
            # Mode catalog wins over baked route cost-per-km.
            cpk = resolve_sample_transport_cpk(chosen, self.config)
            if cpk <= 0:
                cpk = fallback_cpk
            fixed = float(getattr(chosen, "fixed_trip_cost", None) or 0)
            freq = int(getattr(chosen, "frequency_per_week", None) or 1)
            return cpk, fixed, max(0, freq)
        return resolve_sample_transport_cpk(None, self.config), default_fixed, 1

    # ──────────────────────────────────────────────────────────────────────────
    # Sample transport legs (single billing source for card + all breakdowns)
    # ──────────────────────────────────────────────────────────────────────────

    def _primary_referrals_by_facility(self, state: NetworkState) -> Dict[str, Any]:
        """
        One referral per referring facility (first seen).

        Matches ``results_exporter._primary_referral_by_facility`` and the Excel Routes
        sheet — one primary leg per origin, not last-wins over duplicate referrals.
        """
        out: Dict[str, Any] = {}
        for ref in getattr(state, "referrals", None) or []:
            fid = getattr(ref, "facility_id", None)
            if not fid:
                continue
            sfid = str(fid)
            if sfid not in out:
                out[sfid] = ref
        return out

    def iter_sample_transport_legs(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Iterator[SampleTransportLeg]:
        """
        Yield one row per billable primary referral leg.

        Single source of truth for sample-transport billing:

        - **Referral pick:** first-seen per origin (see ``_primary_referrals_by_facility``).
        - **Distance for ₹:** ``_referral_distance × circuity`` (geodesic fallback
          when stored km is zero, matching Routes display). Onsite / zero distance
          → leg omitted (cost 0).
        - **Volume:** ``_hf_sample_transport_volume_detail`` — IS (MaxFreq × W) < demand?
        - **cpk:** matched active route for origin→lab, else Modes catalog / config.

        When there are no referral objects, falls back to one leg per origin from
        ``transport_routes`` (same rules as legacy routes-table path).
        """
        refs = getattr(state, "referrals", None) or []
        if refs:
            yield from self._iter_sample_transport_legs_from_referrals(
                state,
                lab_map=lab_map,
            )
        else:
            yield from self._iter_sample_transport_legs_from_routes_table(state)

    def _iter_sample_transport_legs_from_referrals(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Iterator[SampleTransportLeg]:
        """
        Per referring facility: cost_per_km × distance × volume (referral graph present).

        Same formula as ``network_sample_transport_cost_annual`` when referrals exist;
        extracted here so admin / by-mode / lab aggregators share identical leg costs.
        """
        by_facility = self._primary_referrals_by_facility(state)
        routes = getattr(state, "transport_routes", None) or []

        # Build per-facility route lookup: facility_id → best route (prefer exact lab match)
        route_by_facility: Dict[str, Any] = {}
        for route in routes:
            if not getattr(route, "is_active", True):
                continue
            fid = str(getattr(route, "origin_facility_id", "") or "")
            if not fid:
                continue
            if fid not in route_by_facility:
                route_by_facility[fid] = route

        recost = self._recost_modes_active()
        blended_cpk = self._blended_mode_cpk() if recost else resolve_sample_transport_cpk(None, self.config)
        referrals_using_route_cost = 0
        referrals_using_fallback_cost = 0
        total_annual_volume = 0

        onsite_skip = lab_map is not None

        for fid, ref in by_facility.items():
            if onsite_skip and _is_onsite_referral(
                fid, ref, lab_map, state=state
            ):
                continue
            # Billing distance: stored km with geodesic fallback (matches Routes display)
            dist = _referral_distance(state, ref) * self._distance_circuity
            if dist <= 0:
                continue  # onsite — no transport cost

            max_freq = self._max_ship_frequency_per_week(state, fid)
            initial_route = route_by_facility.get(fid)
            matched_route_found = initial_route is not None
            route = initial_route
            lab_id = str(getattr(ref, "lab_id", "") or "")
            if recost:
                cpk = blended_cpk
                fallback_used = False
                referrals_using_route_cost += 1
            elif route is not None:
                # Prefer exact lab match within this facility's routes
                for r in routes:
                    if (
                        getattr(r, "is_active", True)
                        and str(getattr(r, "origin_facility_id", "")) == fid
                        and str(getattr(r, "dest_facility_id", "")) == lab_id
                    ):
                        route = r
                        break
                route_cpk_raw = float(getattr(route, "cost_per_km", None) or 0)
                # Mode catalog wins over baked route cost-per-km.
                cpk = resolve_sample_transport_cpk(route, self.config) or fallback_cpk
                fallback_used = route_cpk_raw <= 0
                if route_cpk_raw > 0:
                    referrals_using_route_cost += 1
                else:
                    referrals_using_fallback_cost += 1
            else:
                cpk = resolve_sample_transport_cpk(None, self.config)
                fallback_used = True
                referrals_using_fallback_cost += 1

            hf_demand = int(state.test_volumes.get(fid, 0) or 0)
            vol, _ = self._hf_sample_transport_volume_detail(
                hf_demand, max_freq
            )
            total_annual_volume += vol
            leg_cost = float(dist * cpk * vol)

            yield SampleTransportLeg(
                origin_fid=fid,
                lab_id=lab_id,
                cost=leg_cost,
                volume=int(vol),
                dist_bill=dist,
                cpk=float(cpk),
            )

    def _iter_sample_transport_legs_from_routes_table(
        self,
        state: NetworkState,
    ) -> Iterator[SampleTransportLeg]:
        """
        Sample transport when there are no referral objects.

        One charge per referring facility (non-onsite routes only), using the same
        HF demand vs MaxFreq×W if/else as the referral path.
        """
        routes = getattr(state, "transport_routes", None) or []
        by_origin: Dict[str, List[Any]] = defaultdict(list)
        recost = self._recost_modes_active()
        blended_cpk = self._blended_mode_cpk() if recost else resolve_sample_transport_cpk(None, self.config)
        for route in routes:
            if not getattr(route, "is_active", True):
                continue
            oid = str(getattr(route, "origin_facility_id", "") or "")
            did = str(getattr(route, "dest_facility_id", "") or "")
            if not oid or oid == did:
                continue
            if float(route.distance_km or 0) <= 0:
                continue
            by_origin[oid].append(route)

        total_annual_volume = 0
        total = 0.0
        for fid, rlist in by_origin.items():
            max_freq = self._max_ship_frequency_per_week(state, fid)
            # Primary route = highest frequency_per_week among origin's active legs
            primary = max(
                rlist,
                key=lambda r: int(getattr(r, "frequency_per_week", None) or 1),
            )
            dist = float(primary.distance_km or 0) * self._distance_circuity
            if recost:
                cpk = blended_cpk
            else:
                # Mode catalog wins over baked route cost-per-km.
                cpk = resolve_sample_transport_cpk(primary, self.config) or fallback_cpk
            hf_demand = int(state.test_volumes.get(fid, 0) or 0)
            vol, _ = self._hf_sample_transport_volume_detail(hf_demand, max_freq)
            total_annual_volume += vol
            leg_cost = float(dist * cpk * vol)
            total += leg_cost
            yield SampleTransportLeg(
                origin_fid=fid,
                lab_id=str(getattr(primary, "dest_facility_id", "") or ""),
                cost=leg_cost,
                volume=int(vol),
                dist_bill=dist,
                cpk=float(cpk),
            )

    def sample_transport_cost_by_admin_area(
        self,
        state: NetworkState,
        fac_to_a1: Mapping[str, str],
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, float]:
        """
        Group billable legs by origin facility Admin Area 1.

        Used by ``compute_admin_economics`` so Admin Area Analysis transport ₹ sums
        to ``network_sample_transport_cost_annual`` (Transport card). Display columns
        (ASD, transport distance) still use geodesic in the exporter loop.
        """
        by_admin: Dict[str, float] = defaultdict(float)
        for leg in self.iter_sample_transport_legs(state, lab_map=lab_map):
            a1 = fac_to_a1.get(leg.origin_fid, "Unknown")
            by_admin[a1] += leg.cost
        return {k: round(v, 2) for k, v in by_admin.items()}

    def sample_transport_billing_by_leg(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Dict[Tuple[str, str], float]:
        """
        Map (origin_fid, lab_id) → unrounded annual transport cost for billing.

        Used by ``build_transport_rows`` so Routes / by-mode totals reconcile with
        the Transport card; row ``distance_km`` is road-equivalent (geodesic × circuity).
        """
        return {
            (leg.origin_fid, leg.lab_id): leg.cost
            for leg in self.iter_sample_transport_legs(state, lab_map=lab_map)
        }
    
    def _network_sample_transport_cost_from_routes_table(
        self,
        state: NetworkState,
    ) -> Decimal:
        """
        Sample transport when there are no referral objects: one charge per referring
        facility (non-onsite routes only), using the same HF demand vs MaxFreq×W if/else.
        """
        # Delegate to shared leg iterator — network total = sum of leg costs
        total = Decimal(
            str(
                sum(
                    leg.cost
                    for leg in self._iter_sample_transport_legs_from_routes_table(state)
                )
            )
        )
        return total

    def _network_sample_transport_cost_from_referrals(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Decimal:
        """
        Per referring facility: **cost_per_km × distance × volume**, where ``volume`` is:

        IS (HF MaxFreq × W) < HF Total Demand?  (W = ``transport_model_weeks_per_year``)

        - If **yes**: volume = HF MaxFreq × W
        - If **no**: volume = HF Total Demand

        ``cost_per_km`` comes from the active transport route for that facility→lab leg
        (data); only if missing does it fall back to ``EngineConfig.cost_per_km``.
        """
        return Decimal(
            str(
                sum(
                    leg.cost
                    for leg in self._iter_sample_transport_legs_from_referrals(
                        state,
                        lab_map=lab_map,
                    )
                )
            )
        )

    def network_sample_transport_cost_by_lab(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, float]:
        """
        Annual sample transport cost attributed to destination lab
        (same legs as ``network_sample_transport_cost_annual``).
        """
        by_lab: Dict[str, float] = defaultdict(float)
        for leg in self.iter_sample_transport_legs(state, lab_map=lab_map):
            if leg.lab_id:
                by_lab[leg.lab_id] += leg.cost
        # Round per lab for dashboard display; network total rounds once on the card
        return {k: round(v, 2) for k, v in by_lab.items()}

    def network_sample_transport_cost_by_origin_facility(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, float]:
        """
        Annual sample transport cost attributed to the referring (origin) facility.
        """
        by_f: Dict[str, float] = defaultdict(float)
        for leg in self.iter_sample_transport_legs(state, lab_map=lab_map):
            by_f[leg.origin_fid] += leg.cost
        return {k: round(v, 2) for k, v in by_f.items()}

    def network_sample_transport_cost_annual(
        self,
        state: NetworkState,
        *,
        lab_map: Optional[Dict[str, str]] = None,
    ) -> Decimal:
        """
        Annual sample logistics cost (pickup / shuttle from referrers to labs).

        When the network has referral linkages, cost uses each referral's current
        distance and assigned lab so optimisation (redesign) changes the total.
        If there are no referrals, falls back to summing `transport_routes` only.

        Per referring facility: cost_per_km × distance × volume, with volume from the
        HF MaxFreq×W vs Total Demand if/else (see ``_hf_sample_transport_volume``).
        """
        refs = getattr(state, "referrals", None) or []
        if refs:
            return self._network_sample_transport_cost_from_referrals(
                state, lab_map=lab_map
            )
        return self._network_sample_transport_cost_from_routes_table(state)
    
    # ──────────────────────────────────────────────────────────────────────────
    # Network Costs
    # ──────────────────────────────────────────────────────────────────────────
    
    def network_operational_cost_annual(self, state: NetworkState) -> Decimal:
        """
        Calculate total annual operational cost for entire network.
        """
        total = Decimal("0")
        for device in state.devices.values():
            if is_device_operational_for_costing(device):
                total += self.device_operational_cost_annual(device)
        return total
    
    def network_total_cost_annual(
        self,
        state: NetworkState,
        include_startup: bool = True
    ) -> Decimal:
        """
        Calculate total annual cost for entire network.
        
        Includes: operational + testing + one-time startup (summed for ``is_new`` devices)
        """
        operational = self.network_operational_cost_annual(state)
        testing = self.network_testing_cost_annual(state.devices, state.test_volumes, state)
        sample_transport = self.network_sample_transport_cost_annual(state)
        
        startup = Decimal("0")
        if include_startup:
            for device in state.devices.values():
                if getattr(device, "is_new", False):
                    startup += self.device_annualized_startup_cost(device)
        
        return operational + testing + startup + sample_transport
    
    # ──────────────────────────────────────────────────────────────────────────
    # Cost Breakdown
    # ──────────────────────────────────────────────────────────────────────────
    
    def calculate_cost_breakdown(self, state: NetworkState) -> Dict[str, Decimal]:
        """
        Calculate detailed cost breakdown for network.
        
        Returns dict with:
        - operational_cost
        - testing_cost
        - startup_cost_annualized (sum of one-time startup for ``is_new`` devices; name kept for API compatibility)
        - sample_transport_cost
        - total_cost
        """
        operational = self.network_operational_cost_annual(state)
        testing = self.network_testing_cost_annual(state.devices, state.test_volumes, state)
        # Only count startup for devices newly added in this scenario (full one-time cost each).
        # Existing baseline devices have already been installed; their startup cost is sunk.
        startup = sum(
            self.device_annualized_startup_cost(d)
            for d in state.devices.values()
            if getattr(d, "is_new", False)
        )
        sample_transport = self.network_sample_transport_cost_annual(state)
        
        return {
            "operational_cost": operational,
            "testing_cost": testing,
            "startup_cost_annualized": startup,
            "sample_transport_cost": sample_transport,
            "total_cost": operational + testing + startup + sample_transport,
        }
    
    def cost_per_test(self, state: NetworkState) -> Decimal:
        """
        Calculate average cost per test across network.
        """
        total_cost = self.network_total_cost_annual(state)
        total_tests = sum(state.test_volumes.values())
        
        if total_tests == 0:
            return Decimal("0")
        
        return total_cost / total_tests
