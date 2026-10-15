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
Referral models for facility-to-lab referral linkages.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class Referral:
    """
    Historical referral linkage between facility and lab.
    
    Attributes:
        facility_id: Source facility sending samples
        lab_id: Destination lab processing samples
        referral_count: Historical number of referrals
        distance_km: Distance between facility and lab
        is_synthetic: False for historical referrals
    """
    
    facility_id: str
    lab_id: str
    referral_count: int = 0
    distance_km: float = 0.0
    
    # This is historical data, not synthetic
    is_synthetic: bool = False
    confidence: float = 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "facility_id": self.facility_id,
            "lab_id": self.lab_id,
            "referral_count": self.referral_count,
            "distance_km": self.distance_km,
            "is_synthetic": self.is_synthetic,
            "confidence": self.confidence,
        }


@dataclass
class SyntheticReferral:
    """
    Generated referral linkage when historical data is missing.
    
    Used when:
    - Historical referrals table is empty or incomplete
    - Specific facilities have no referral history
    - New facilities added without historical data
    
    Generation algorithm considers:
    1. Distance (nearest lab preferred)
    2. Capacity availability at lab
    3. Administrative boundary alignment
    4. Facility tier compatibility
    
    Attributes:
        facility_id: Source facility
        lab_id: Target lab
        distance_km: Calculated distance
        confidence: Score from 0.0 to 1.0
        is_synthetic: Always True
        generation_method: Algorithm used to generate
        admin_boundary_crossed: Whether referral crosses admin boundary
    """
    
    facility_id: str
    lab_id: str
    distance_km: float
    confidence: float  # 0.0 to 1.0
    
    is_synthetic: bool = True
    generation_method: str = "distance_capacity_admin"
    admin_boundary_crossed: bool = False
    
    # Scoring components
    distance_score: float = 0.0
    capacity_score: float = 0.0
    admin_score: float = 0.0
    tier_score: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "facility_id": self.facility_id,
            "lab_id": self.lab_id,
            "distance_km": self.distance_km,
            "confidence": self.confidence,
            "is_synthetic": self.is_synthetic,
            "generation_method": self.generation_method,
            "admin_boundary_crossed": self.admin_boundary_crossed,
            "scores": {
                "distance": self.distance_score,
                "capacity": self.capacity_score,
                "admin": self.admin_score,
                "tier": self.tier_score,
            },
        }
    
    @classmethod
    def calculate_weighted_score(
        cls,
        distance_score: float,
        capacity_score: float,
        admin_score: float,
        tier_score: float,
        weights: Optional[Dict[str, float]] = None
    ) -> float:
        """
        Calculate weighted confidence score.
        
        Default weights:
        - Distance: 0.4
        - Capacity: 0.3
        - Admin alignment: 0.2
        - Tier compatibility: 0.1
        
        Args:
            distance_score: Distance-based score (0-1)
            capacity_score: Capacity availability score (0-1)
            admin_score: Admin boundary alignment score (0-1)
            tier_score: Tier compatibility score (0-1)
            weights: Optional custom weights
            
        Returns:
            Weighted confidence score between 0.0 and 1.0
        """
        if weights is None:
            weights = {
                "distance": 0.4,
                "capacity": 0.3,
                "admin": 0.2,
                "tier": 0.1,
            }
        
        score = (
            weights["distance"] * distance_score +
            weights["capacity"] * capacity_score +
            weights["admin"] * admin_score +
            weights["tier"] * tier_score
        )
        
        return max(0.0, min(1.0, score))


# Legacy DB default on transport route cost_per_km — commonly means "unset"
_TRANSPORT_ROUTE_COST_PER_KM_DB_DEFAULT = 0.10
_LAST_RESORT_SAMPLE_TRANSPORT_CPK = 50.0
_PLACEHOLDER_MODE_LABELS = {"", "ROAD"}


def _catalog_entry(config: Any, mode_id: Optional[str]) -> Dict[str, Any]:
    catalog = getattr(config, "sample_transport_mode_catalog", None) or {}
    return dict(catalog.get(str(mode_id or ""), {}) or {})


def catalog_default_mode_name(config: Any) -> Optional[str]:
    """Primary Modes-tab label for export / synthetic route stamping."""
    default_id = getattr(config, "default_sample_transport_mode_id", None)
    if default_id:
        name = str(_catalog_entry(config, str(default_id)).get("mode") or "").strip()
        if name:
            return name
    catalog = getattr(config, "sample_transport_mode_catalog", None) or {}
    if len(catalog) == 1:
        name = str(next(iter(catalog.values())).get("mode") or "").strip()
        if name:
            return name
    return None


def _catalog_cpk_by_mode_name(config: Any, mode_name: str) -> float:
    label = str(mode_name or "").strip()
    if not label or label.upper() in _PLACEHOLDER_MODE_LABELS:
        return 0.0
    key = label.casefold()
    catalog = getattr(config, "sample_transport_mode_catalog", None) or {}
    for entry in catalog.values():
        if str(entry.get("mode") or "").strip().casefold() == key:
            return float(entry.get("cost_per_km") or 0)
    return 0.0


def _default_catalog_cpk(config: Any) -> float:
    default_id = getattr(config, "default_sample_transport_mode_id", None)
    if default_id:
        cpk = float(_catalog_entry(config, str(default_id)).get("cost_per_km") or 0)
        if cpk > 0:
            return cpk
    catalog = getattr(config, "sample_transport_mode_catalog", None) or {}
    if len(catalog) == 1:
        cpk = float(next(iter(catalog.values())).get("cost_per_km") or 0)
        if cpk > 0:
            return cpk
    return 0.0


def resolve_sample_transport_cpk(route: Optional[Any], config: Any) -> float:
    """
    Single billing cpk resolver: Modes tab first, EngineConfig sync, 50 last resort.

    Used by sample-transport legs, MILP financial terms, and synthetic route birth.

    Priority when transport_mode_id is set: mode catalog rate always wins over a
    baked-in route cpk — prevents a stale baseline rate from shadowing the correct
    rate after a scenario mode change. route_cpk is only used as a fallback when the
    mode has no catalog entry (e.g., custom modes not yet in the Modes tab).
    """
    if route is not None:
        route_cpk = float(getattr(route, "cost_per_km", None) or 0)
        mode_id = getattr(route, "transport_mode_id", None)
        mode_name = str(getattr(route, "transport_mode", "") or "")

        # Mode catalog takes priority when mode_id is set — avoids stale baked cpk override.
        if mode_id:
            cpk = float(_catalog_entry(config, str(mode_id)).get("cost_per_km") or 0)
            if cpk > 0:
                return cpk

        # Non-placeholder route cpk as fallback (explicit override or mode has no catalog entry).
        if route_cpk > 0 and abs(route_cpk - _TRANSPORT_ROUTE_COST_PER_KM_DB_DEFAULT) >= 1e-9:
            return route_cpk

        cpk = _catalog_cpk_by_mode_name(config, mode_name)
        if cpk > 0:
            return cpk

        if route_cpk > 0:
            return route_cpk

    cpk = _default_catalog_cpk(config)
    if cpk > 0:
        return cpk

    synced = float(getattr(config, "cost_per_km", 0) or 0)
    if synced > 0:
        return synced

    logger.warning(
        "resolve_sample_transport_cpk: no Modes catalog cpk; using last-resort %.1f",
        _LAST_RESORT_SAMPLE_TRANSPORT_CPK,
    )
    return _LAST_RESORT_SAMPLE_TRANSPORT_CPK


def stamp_scenario_route_from_catalog(route: Any, config: Any) -> None:
    """Stamp synthetic scenario routes from Modes tab when patch omitted economics."""
    if route is None or config is None:
        return
    cpk = resolve_sample_transport_cpk(route, config)
    name = catalog_default_mode_name(config)
    mode_label = str(getattr(route, "transport_mode", "") or "").strip().upper()
    if name and mode_label in _PLACEHOLDER_MODE_LABELS:
        route.transport_mode = name
    default_id = getattr(config, "default_sample_transport_mode_id", None)
    if default_id and not getattr(route, "transport_mode_id", None):
        route.transport_mode_id = str(default_id)
    if float(getattr(route, "cost_per_km", None) or 0) <= 0:
        route.cost_per_km = cpk


def effective_transport_cost_per_km_from_db_row(row: Any) -> float:
    """
    Resolve INR/km for a ``dno_transport_routes`` row so the engine matches baseline metrics.

    Baseline transport-by-mode uses ``ref_transport_modes.cost_per_km``. The cost calculator
    reads ``TransportRoute.cost_per_km``; when that is **0** (null in DB), it falls back to
    ``EngineConfig.cost_per_km`` (schema default **50** INR/km) — so missing route rates inflate
    sample transport vs the catalog (e.g. Pune **~15.34**). Route rows may also still have the
    legacy schema default **0.10**, which deflates costs; prefer the linked mode when the route looks
    unset. Non-placeholder positive values on the route are treated as explicit overrides.
    """
    mode = getattr(row, "ref_transport_modes", None)
    mode_cpk = float(mode.cost_per_km or 0) if mode else 0.0
    route_cpk = float(getattr(row, "cost_per_km", None) or 0)
    if mode_cpk > 0 and (
        route_cpk <= 0
        or abs(route_cpk - _TRANSPORT_ROUTE_COST_PER_KM_DB_DEFAULT) < 1e-9
    ):
        return mode_cpk
    if route_cpk > 0:
        return route_cpk
    return mode_cpk if mode_cpk > 0 else 0.0


@dataclass
class TransportRoute:
    """
    Transport route between facilities.
    """
    
    id: str
    project_id: str
    origin_facility_id: str
    dest_facility_id: str
    
    distance_km: float = 0.0
    transit_time_minutes: int = 0
    transport_mode: str = "ROAD"
    transport_mode_id: Optional[str] = None

    cost_per_km: float = 0.0
    fixed_trip_cost: float = 0.0
    frequency_per_week: int = 1
    test_type_id: Optional[str] = None
    
    is_active: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def total_trip_cost(self) -> float:
        """Calculate total cost for one trip."""
        return (self.distance_km * self.cost_per_km) + self.fixed_trip_cost
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "origin_facility_id": self.origin_facility_id,
            "dest_facility_id": self.dest_facility_id,
            "distance_km": self.distance_km,
            "transit_time_minutes": self.transit_time_minutes,
            "transport_mode": self.transport_mode,
            "total_trip_cost": self.total_trip_cost,
        }


def _self_check_sample_transport_cpk() -> None:
    """Laos QA row: empty route + Health Staff @ 0.3 must not bill at 50."""
    from decimal import Decimal
    from dno_core.models.config import EngineConfig

    cfg = EngineConfig(project_id="p")
    cfg.sample_transport_mode_catalog = {
        "m1": {"mode": "Health Staff", "cost_per_km": 0.3, "speed_kmph": 40.0},
    }
    cfg.default_sample_transport_mode_id = "m1"
    cfg.cost_per_km = Decimal("50")

    route = TransportRoute(
        id="r1",
        project_id="p",
        origin_facility_id="hf",
        dest_facility_id="lab",
        distance_km=43.9436,
    )
    cpk = resolve_sample_transport_cpk(route, cfg)
    assert abs(cpk - 0.3) < 1e-9, f"expected 0.3 got {cpk}"
    assert abs(43.9436 * 0.3 - 13.18308) < 1e-4

    explicit = TransportRoute(
        id="r2",
        project_id="p",
        origin_facility_id="hf",
        dest_facility_id="lab",
        cost_per_km=15.34,
    )
    assert abs(resolve_sample_transport_cpk(explicit, cfg) - 15.34) < 1e-9


if __name__ == "__main__":
    _self_check_sample_transport_cpk()
    print("referral sample-transport cpk self-check OK")
