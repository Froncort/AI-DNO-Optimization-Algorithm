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
Shared transport/referral helpers used by cost billing and results export.

Kept in a small module so ``cost.py`` and ``results_exporter.py`` never import each other.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from dno_core.models.network_state import NetworkState

logger = logging.getLogger(__name__)


def parse_route_annual_samples(metadata: Any) -> Optional[int]:
    """Coerce route metadata annual_samples; None when absent or malformed."""
    if not metadata or not isinstance(metadata, dict):
        return None
    raw = metadata.get("annual_samples")
    if raw is None:
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    # 0 = ingest default / empty column — not a per-edge override; HF demand is canonical.
    return v if v > 0 else None


def _route_metadata_dict(route: Any) -> Dict[str, Any]:
    meta = getattr(route, "metadata", None)
    if isinstance(meta, dict):
        return meta
    if meta is not None and hasattr(meta, "__dict__"):
        return dict(meta)
    return {}


def route_sample_volume(
    state: NetworkState,
    origin_id: str,
    dest_id: str,
    test_type_id: Optional[str] = None,
) -> Optional[int]:
    """Read annual_samples from matching active transport route row."""
    oid, did = str(origin_id), str(dest_id)
    tt = str(test_type_id) if test_type_id else None
    for route in state.transport_routes or []:
        if not getattr(route, "is_active", True):
            continue
        if str(getattr(route, "origin_facility_id", "")) != oid:
            continue
        if str(getattr(route, "dest_facility_id", "")) != did:
            continue
        route_tid = getattr(route, "test_type_id", None)
        if tt is not None:
            if route_tid is None or str(route_tid) != tt:
                continue
        elif route_tid is not None:
            # Untyped lookup skips typed rows when caller wants aggregate.
            continue
        vol = parse_route_annual_samples(_route_metadata_dict(route))
        if vol is not None:
            return vol
    return None


def origin_route_sample_volume_total(
    state: NetworkState,
    origin_id: str,
    dest_id: str,
) -> Optional[int]:
    """Sum annual_samples across all typed route rows on one physical lane."""
    oid, did = str(origin_id), str(dest_id)
    total = 0
    found = False
    for route in state.transport_routes or []:
        if not getattr(route, "is_active", True):
            continue
        if str(getattr(route, "origin_facility_id", "")) != oid:
            continue
        if str(getattr(route, "dest_facility_id", "")) != did:
            continue
        vol = parse_route_annual_samples(_route_metadata_dict(route))
        if vol is not None:
            total += vol
            found = True
    return total if found else None


def _resolve_lab_facility_id(lab_id: str, lab_map: Optional[Dict[str, str]]) -> str:
    """Map dno_labs.id to dno_labs.facility_id when present; else pass through."""
    if not lab_map:
        return lab_id
    return lab_map.get(lab_id, lab_id)


def _referral_distance(state: NetworkState, referral: Any) -> float:
    facility = (
        state.facilities.get(referral.facility_id)
        or state.labs.get(referral.facility_id)
    )
    lab = state.labs.get(referral.lab_id) or state.facilities.get(referral.lab_id)
    if not facility or not lab:
        return 0.0
    d = float(getattr(referral, "distance_km", None) or 0)
    if d == 0 and facility.has_coordinates and lab.has_coordinates:
        # Local import avoids loading calculators.__init__ while cost.py is importing this module.
        from dno_core.calculators.distance import DistanceCalculator

        d = float(DistanceCalculator.facility_geodesic_km(facility, lab) or 0)
    return d


def _is_onsite_referral(
    referring_facility_id: str,
    ref: Any,
    lab_map: Optional[Dict[str, str]] = None,
    *,
    state: Optional[NetworkState] = None,
) -> bool:
    """
    Onsite when the referring facility is the lab site:
    1. Strict self-edge (fid == lab_id).
    2. Referring facility_id matches dno_labs.facility_id via lab_map (lab row UUID).
    3. Geodesic distance between facility and lab is exactly 0 km — same physical campus
       stored as two separate facility rows (no name-based inference; only coordinates).
    """
    fid = str(referring_facility_id)
    lid = str(ref.lab_id)
    resolved = _resolve_lab_facility_id(lid, lab_map)
    if fid == lid or fid == resolved:
        return True
    # Co-location: same coordinates but different IDs
    if state is not None and _referral_distance(state, ref) == 0.0:
        return True
    return False


def origin_demand_fallback(
    test_volumes: Optional[Dict[str, int]], origin_id: str
) -> int:
    """HF aggregate demand when route rows lack annual_samples metadata."""
    if not test_volumes:
        return 0
    return int(test_volumes.get(str(origin_id), 0) or 0)


def aggregate_hf_lab_referrals(
    routes: List[Any],
    facilities: Union[Dict[str, Any], Set[str]],
    labs: Union[Dict[str, Any], Set[str]],
    *,
    test_volumes: Optional[Dict[str, int]] = None,
) -> List[Any]:
    """
    One Referral per (HF, Lab) pair; referral_count = sum of route annual_samples.
    ponytail: O(routes) single pass — fine for typical project sizes.
    """
    from dno_core.models.referral import Referral

    fac_keys = set(facilities.keys()) if isinstance(facilities, dict) else set(facilities)
    lab_keys = set(labs.keys()) if isinstance(labs, dict) else set(labs)

    # (origin, dest) -> volume sum + first-seen distance
    pair_data: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for route in routes or []:
        origin_id = str(getattr(route, "origin_facility_id", ""))
        dest_id = str(getattr(route, "dest_facility_id", ""))
        if origin_id not in fac_keys or dest_id not in lab_keys or origin_id in lab_keys:
            continue

        pair = (origin_id, dest_id)
        vol = parse_route_annual_samples(_route_metadata_dict(route))
        if pair not in pair_data:
            pair_data[pair] = {
                "volume_sum": 0,
                "has_metadata": False,
                "distance_km": float(getattr(route, "distance_km", 0) or 0),
            }
        if vol is not None:
            pair_data[pair]["volume_sum"] += vol
            pair_data[pair]["has_metadata"] = True

    referrals: List[Any] = []
    for (origin_id, dest_id), data in pair_data.items():
        if data["has_metadata"]:
            count = int(data["volume_sum"])
        else:
            count = origin_demand_fallback(test_volumes, origin_id)
        referrals.append(
            Referral(
                facility_id=origin_id,
                lab_id=dest_id,
                referral_count=count,
                distance_km=data["distance_km"],
                is_synthetic=False,
                confidence=1.0,
            )
        )
    return referrals


def is_lab_referrable(state: NetworkState, lab_id: str) -> bool:
    """Lab can receive referred samples: in state.labs with operational device capacity."""
    if lab_id not in state.labs:
        return False
    wd = state.config.working_days_per_year if state.config else 300
    from dno_core.calculators.shift_capacity import per_test_capacity_at_lab

    caps = per_test_capacity_at_lab(state, lab_id, wd)
    return sum(caps.values()) > 0


def route_endpoints_from_baseline(
    state: NetworkState, baseline_route_id: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve HF→lab pair from baseline transport route id (scenario referral patches)."""
    if not baseline_route_id:
        return None, None
    rid = str(baseline_route_id)
    for route in state.transport_routes or []:
        if str(getattr(route, "id", "")) == rid:
            return (
                str(getattr(route, "origin_facility_id", "") or "") or None,
                str(getattr(route, "dest_facility_id", "") or "") or None,
            )
    return None, None


# Engine route patch tag — cleared on re-optimize with REDESIGN rows.
HF_CONVERSION_ROUTE_DELETE = "HF_CONVERSION_ROUTE_DELETE"


def promote_facility_to_lab(state: NetworkState, facility: Any) -> NetworkState:
    """
    Facility→lab: move node to labs and drop stale outbound referral.
    Lab origins process onsite via test_volumes (logical_relations_dno.md §1.1).
    """
    fid = str(facility.id)
    new_state = state.with_new_lab(facility)
    new_state.referrals = [
        r for r in (new_state.referrals or [])
        if str(r.facility_id) != fid
    ]
    new_state._referrals_by_facility = None
    return new_state


def drop_outbound_referrals_for_lab_origins(state: NetworkState) -> NetworkState:
    """Remove referrals whose origin is a lab (onsite processing — no outbound edge)."""
    lab_ids = {str(lid) for lid in (state.labs or {})}
    if not lab_ids or not state.referrals:
        return state
    before = len(state.referrals)
    state.referrals = [
        r for r in state.referrals if str(r.facility_id) not in lab_ids
    ]
    dropped = before - len(state.referrals)
    if dropped:
        logger.info(
            "Dropped %s stale outbound referral(s) for lab-origin site(s)",
            dropped,
        )
        state._referrals_by_facility = None
    return state


def baseline_route_ids_for_origin(
    state: NetworkState, facility_id: str
) -> List[str]:
    """All baseline transport route ids with this origin (multi-lane safe)."""
    oid = str(facility_id)
    ids: List[str] = []
    for route in state.transport_routes or []:
        if str(getattr(route, "origin_facility_id", "")) != oid:
            continue
        rid = str(getattr(route, "id", "") or "")
        if rid:
            ids.append(rid)
    if len(ids) > 1:
        logger.info(
            "Multi-lane origin %s: %s baseline route row(s) for conversion DELETE",
            oid[:8],
            len(ids),
        )
    return ids


def find_nearest_referrable_lab(
    facility: Any,
    state: NetworkState,
    exclude_lab_ids: Optional[Set[str]] = None,
) -> Optional[Any]:
    """Closest lab with operational capacity; skips deleted or dead labs."""
    from dno_core.calculators.distance import DistanceCalculator

    exclude = exclude_lab_ids or set()
    nearest: Optional[Any] = None
    min_distance = float("inf")
    for lab_id, lab in state.labs.items():
        if lab_id in exclude or not is_lab_referrable(state, lab_id):
            continue
        distance = DistanceCalculator.facility_distance(facility, lab)
        if distance is not None and distance < min_distance:
            min_distance = distance
            nearest = lab
    return nearest


def reconcile_referrals(state: NetworkState) -> NetworkState:
    """
    Drop referrals to removed or zero-capacity labs; redirect origins to nearest referrable lab.
    Canonical referral-plane cleanup after scenario patches (see logical_relations_dno.md §3).
    """
    if not state.referrals:
        return state

    bad_pairs: Set[Tuple[str, str]] = set()
    need_redirect: Set[str] = set()

    for ref in state.referrals:
        fac_ok = ref.facility_id in state.facilities
        lab_ok = is_lab_referrable(state, ref.lab_id)
        if fac_ok and lab_ok:
            continue
        bad_pairs.add((ref.facility_id, ref.lab_id))
        if fac_ok:
            need_redirect.add(ref.facility_id)

    if not bad_pairs:
        return state

    state.referrals = [
        r for r in state.referrals if (r.facility_id, r.lab_id) not in bad_pairs
    ]
    assigned = {r.facility_id for r in state.referrals}

    for fid in need_redirect:
        if fid in assigned:
            continue
        fac = state.facilities.get(fid)
        if not fac:
            continue
        dest = find_nearest_referrable_lab(fac, state)
        if not dest:
            logger.warning(
                "Reconcile: no referrable lab for facility %s after dead-lab cleanup",
                fid[:8],
            )
            continue
        state = state.with_referral_changed(fid, dest.id)
        assigned.add(fid)
        logger.info(
            "Reconcile: redirected %s → %s (former lab removed or non-referrable)",
            fid[:8],
            dest.id[:8],
        )

    state._referrals_by_facility = None
    return state


if __name__ == "__main__":
    # ponytail: minimal self-check — dead lab loses inbound referral after reconcile
    from dno_core.models.facility import Facility
    from dno_core.models.referral import Referral
    from dno_core.models.device import DeviceInventory
    from dno_core.models.config import EngineConfig

    hf = Facility(
        id="hf1", project_id="p", name="HF", latitude=1.0, longitude=1.0,
        facility_tier="LEVEL_3", admin_area_1="A",
    )
    lab = Facility(
        id="lab1", project_id="p", name="Lab", latitude=1.1, longitude=1.1,
        facility_tier="LEVEL_5", admin_area_1="A", is_lab=True,
    )
    lab2 = Facility(
        id="lab2", project_id="p", name="Lab2", latitude=1.2, longitude=1.2,
        facility_tier="LEVEL_5", admin_area_1="A", is_lab=True,
    )
    dev = DeviceInventory(
        device_id="d1", facility_id="lab1", device_type="GX4", device_type_id="dt1",
        max_tests_per_shift=10, max_shifts=1, status="NON_FUNCTIONAL",
    )
    dev2 = DeviceInventory(
        device_id="d2", facility_id="lab2", device_type="GX4", device_type_id="dt1",
        max_tests_per_shift=10, max_shifts=1, status="OPERATIONAL",
    )
    cfg = EngineConfig(project_id="p", working_days_per_year=300)
    st = NetworkState(
        project_id="p",
        facilities={"hf1": hf},
        labs={"lab1": lab, "lab2": lab2},
        devices={"d1": dev, "d2": dev2},
        referrals=[Referral(facility_id="hf1", lab_id="lab1")],
        test_volumes={"hf1": 100},
        config=cfg,
    )
    assert is_lab_referrable(st, "lab1") is False
    assert is_lab_referrable(st, "lab2") is True
    out = reconcile_referrals(st)
    assert out.referrals[0].lab_id == "lab2", out.referrals
    print("reconcile_referrals self-check OK")

    # Facility→lab promotion drops outbound; sanitize clears any lab-origin leftovers.
    hf_conv = Facility(
        id="hf_conv", project_id="p", name="HFConv", latitude=2.0, longitude=2.0,
        facility_tier="LEVEL_3", admin_area_1="A",
    )
    remote_lab = Facility(
        id="remote_lab", project_id="p", name="Remote", latitude=3.0, longitude=3.0,
        facility_tier="LEVEL_5", admin_area_1="A", is_lab=True,
    )
    st_conv = NetworkState(
        project_id="p",
        facilities={"hf_conv": hf_conv},
        labs={"remote_lab": remote_lab},
        referrals=[Referral(facility_id="hf_conv", lab_id="remote_lab", distance_km=22.0)],
        test_volumes={"hf_conv": 74},
        config=cfg,
    )
    promoted = promote_facility_to_lab(st_conv, hf_conv)
    assert "hf_conv" in promoted.labs
    assert "hf_conv" not in promoted.facilities
    assert not any(r.facility_id == "hf_conv" for r in promoted.referrals)
    sanitized = drop_outbound_referrals_for_lab_origins(promoted)
    assert not any(r.facility_id == "hf_conv" for r in sanitized.referrals)
    print("promote_facility_to_lab self-check OK")
