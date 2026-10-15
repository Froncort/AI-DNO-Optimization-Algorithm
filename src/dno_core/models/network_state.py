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
Network state model representing complete diagnostic network snapshot.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, List, Any, Optional, Union, Tuple, Set
from decimal import Decimal

from dno_core.models.device import DeviceInventory
from dno_core.models.facility import Facility
from dno_core.models.referral import Referral, SyntheticReferral, TransportRoute
from dno_core.models.config import EngineConfig


@dataclass
class NetworkState:
    """
    Complete network state snapshot.
    
    Represents the full state of a diagnostic network at a point in time,
    including all facilities, labs, devices, referrals, and test volumes.
    
    This is the primary data structure passed through the optimization pipeline.
    
    Attributes:
        project_id: Project this state belongs to
        facilities: All facilities (non-labs) keyed by ID
        labs: All labs (facilities with devices) keyed by ID
        devices: All atomic devices keyed by device_id
        referrals: All referral linkages (historical or synthetic)
        test_volumes: Historical test volumes by facility_id
        transport_routes: Transport routes between facilities
        config: Engine configuration
    """
    
    project_id: str
    
    # Entities (keyed by ID for fast lookup)
    facilities: Dict[str, Facility] = field(default_factory=dict)
    labs: Dict[str, Facility] = field(default_factory=dict)
    devices: Dict[str, DeviceInventory] = field(default_factory=dict)
    
    # Relationships
    referrals: List[Union[Referral, SyntheticReferral]] = field(default_factory=list)
    transport_routes: List[TransportRoute] = field(default_factory=list)
    
    # Volumes (facility_id → annual test count)
    test_volumes: Dict[str, int] = field(default_factory=dict)

    # Per-test demand (facility_id → test_type_id → annual volume); optional slice channel
    test_volumes_by_type: Optional[Dict[str, Dict[str, int]]] = field(default=None, repr=False)

    # Per-(device_type_id, test_type_id) throughput rows from ref_device_test_parameters
    device_test_parameters_by_type: Optional[Dict[Tuple[str, str], dict]] = field(
        default=None, repr=False
    )

    # Active tests at each lab: (lab_id, test_type_id) — seeded from DB + catalogue, union on integration replay
    facility_test_capabilities: Optional[Set[Tuple[str, str]]] = field(default=None, repr=False)

    # device_id → test_type_ids activated by TEST_INTEGRATION in this scenario
    device_active_tests: Optional[Dict[str, Set[str]]] = field(default=None, repr=False)
    
    # Configuration
    config: Optional[EngineConfig] = None

    # Device type catalogue (name → dict) — attached by scenario loader so every
    # solver can use real specs without a DB call.  Optional; solvers fall back to
    # hardcoded defaults when absent.
    device_types_info: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # ref_test_types id → display name for UI payloads and action labels
    test_type_names: Optional[Dict[str, str]] = field(default=None, repr=False)

    scenario_mad_device_caps: Optional[dict] = field(default=None, repr=False)

    # Historical testing volumes keyed by (facility_id, device_type_id) from dno_historical_testing
    device_type_volumes: Optional[Dict[Tuple[str, str], int]] = field(default=None, repr=False)

    # Per-(facility_id, device_type_id, test_type_id) historical activity for device-active seeding
    device_type_test_volumes: Optional[Dict[Tuple[str, str, str], int]] = field(
        default=None, repr=False
    )

    # When False, scenario route changes / synthetic referrals disable historical lab baseline override.
    use_historical_lab_baseline: bool = field(default=True, repr=False)

    # Computed caches (populated on demand)
    _devices_by_lab: Optional[Dict[str, List[DeviceInventory]]] = field(
        default=None, repr=False
    )
    _referrals_by_facility: Optional[Dict[str, List[Union[Referral, SyntheticReferral]]]] = field(
        default=None, repr=False
    )
    
    def __post_init__(self):
        """Invalidate caches after initialization."""
        self._devices_by_lab = None
        self._referrals_by_facility = None
    
    # ──────────────────────────────────────────────────────────────────────────
    # Entity Access
    # ──────────────────────────────────────────────────────────────────────────
    
    def all_facilities(self) -> List[Facility]:
        """Get all facilities including labs."""
        return list(self.facilities.values()) + list(self.labs.values())
    
    def all_devices(self) -> List[DeviceInventory]:
        """Get all atomic devices."""
        return list(self.devices.values())
    
    def get_devices_by_lab(self, invalidate_cache: bool = False) -> Dict[str, List[DeviceInventory]]:
        """
        Get devices grouped by lab ID.
        
        Args:
            invalidate_cache: Force recalculation
            
        Returns:
            Dict mapping lab_id → list of devices
        """
        if self._devices_by_lab is not None and not invalidate_cache:
            #  O(n) id-set compare — catches in-place device adds during scenario replay
            cached_ids = {
                str(d.device_id)
                for devs in self._devices_by_lab.values()
                for d in devs
            }
            if cached_ids != set(self.devices.keys()):
                self._devices_by_lab = None

        if self._devices_by_lab is None or invalidate_cache:
            self._devices_by_lab = {}
            for device in self.devices.values():
                if device.facility_id not in self._devices_by_lab:
                    self._devices_by_lab[device.facility_id] = []
                self._devices_by_lab[device.facility_id].append(device)
        return self._devices_by_lab
    
    def get_lab_device_count(self, lab_id: str) -> int:
        """Get number of devices at a lab."""
        devices_by_lab = self.get_devices_by_lab()
        return len(devices_by_lab.get(lab_id, []))
    
    def get_lab_device_types(self, lab_id: str) -> Dict[str, int]:
        """Get count of each device type at a lab."""
        devices_by_lab = self.get_devices_by_lab()
        type_counts: Dict[str, int] = {}
        for device in devices_by_lab.get(lab_id, []):
            type_counts[device.device_type] = type_counts.get(device.device_type, 0) + 1
        return type_counts
    
    # ──────────────────────────────────────────────────────────────────────────
    # Referral Access
    # ──────────────────────────────────────────────────────────────────────────
    
    def get_referrals_by_facility(
        self, 
        invalidate_cache: bool = False
    ) -> Dict[str, List[Union[Referral, SyntheticReferral]]]:
        """Get referrals grouped by facility ID."""
        if self._referrals_by_facility is None or invalidate_cache:
            self._referrals_by_facility = {}
            for referral in self.referrals:
                if referral.facility_id not in self._referrals_by_facility:
                    self._referrals_by_facility[referral.facility_id] = []
                self._referrals_by_facility[referral.facility_id].append(referral)
        return self._referrals_by_facility
    
    def get_labs_for_facility(self, facility_id: str) -> List[str]:
        """Get lab IDs that a facility refers to."""
        refs_by_facility = self.get_referrals_by_facility()
        return [r.lab_id for r in refs_by_facility.get(facility_id, [])]
    
    # ──────────────────────────────────────────────────────────────────────────
    # Capacity & Utilization
    # ──────────────────────────────────────────────────────────────────────────
    
    def total_network_capacity(self, working_days: int = 300) -> int:
        """Calculate total annual test capacity across all devices."""
        return sum(d.annual_capacity(working_days) for d in self.devices.values())
    
    def total_network_demand(self) -> int:
        """Calculate total annual test demand."""
        return sum(self.test_volumes.values())
    
    def lab_capacity(self, lab_id: str, working_days: int = 300) -> int:
        """Calculate total capacity at a specific lab."""
        devices_by_lab = self.get_devices_by_lab()
        return sum(d.annual_capacity(working_days) for d in devices_by_lab.get(lab_id, []))
    
    def lab_utilization(self, lab_id: str, working_days: int = 300) -> float:
        """Calculate utilization at a specific lab."""
        capacity = self.lab_capacity(lab_id, working_days)
        if capacity == 0:
            return 0.0
        
        # Sum test volumes for facilities referring to this lab
        volume = sum(
            self.test_volumes.get(r.facility_id, 0)
            for r in self.referrals
            if r.lab_id == lab_id
        )
        
        return volume / capacity
    
    # ──────────────────────────────────────────────────────────────────────────
    # State Mutation (returns new state, immutable pattern)
    # ──────────────────────────────────────────────────────────────────────────
    
    def copy(self) -> "NetworkState":
        """Create a deep copy of this state."""
        # Deep-clone devices so in-place shift rescale does not mutate shared instances
        cloned_devices = {
            did: replace(d, metadata=dict(d.metadata or {}))
            for did, d in self.devices.items()
        }
        return NetworkState(
            project_id=self.project_id,
            facilities=dict(self.facilities),
            labs=dict(self.labs),
            devices=cloned_devices,
            referrals=list(self.referrals),
            transport_routes=list(self.transport_routes),
            test_volumes=dict(self.test_volumes),
            test_volumes_by_type={
                fid: dict(by_type)
                for fid, by_type in self.test_volumes_by_type.items()
            } if self.test_volumes_by_type else None,
            device_test_parameters_by_type=(
                dict(self.device_test_parameters_by_type)
                if self.device_test_parameters_by_type
                else None
            ),
            facility_test_capabilities=(
                set(self.facility_test_capabilities)
                if self.facility_test_capabilities
                else None
            ),
            device_active_tests=(
                {did: set(tests) for did, tests in self.device_active_tests.items()}
                if self.device_active_tests
                else None
            ),
            config=self.config,
            device_types_info=self.device_types_info,
            test_type_names=dict(self.test_type_names) if self.test_type_names else None,
            scenario_mad_device_caps=self.scenario_mad_device_caps,
            device_type_volumes=dict(self.device_type_volumes) if self.device_type_volumes else None,
            device_type_test_volumes=(
                dict(self.device_type_test_volumes) if self.device_type_test_volumes else None
            ),
            use_historical_lab_baseline=self.use_historical_lab_baseline,
        )
    
    def with_device_moved(
        self, 
        device_id: str, 
        new_lab_id: str
    ) -> "NetworkState":
        """
        Return new state with device moved to new lab.
        
        Does NOT modify original state.
        """
        new_state = self.copy()
        if device_id in new_state.devices:
            device = new_state.devices[device_id]
            # Create new device with updated facility_id
            new_device = DeviceInventory(
                facility_id=new_lab_id,
                device_type=device.device_type,
                device_id=device.device_id,
                device_type_id=device.device_type_id,
                quantity=device.quantity,
                max_shifts=device.max_shifts,
                max_tests_per_shift=device.max_tests_per_shift,
                stored_annual_capacity=device.stored_annual_capacity,
                modules=device.modules,
                cost_per_test=device.cost_per_test,
                overhead_cost_annual=device.overhead_cost_annual,
                shift_cost_annual=device.shift_cost_annual,
                startup_cost=device.startup_cost,
                current_utilization=device.current_utilization,
                tests_conducted_annual=device.tests_conducted_annual,
                status=device.status,
            )
            new_state.devices[device_id] = new_device
            new_state._devices_by_lab = None  # Invalidate cache
            
            # If target is not currently a lab, upgrade it
            if new_lab_id in new_state.facilities and new_lab_id not in new_state.labs:
                from dno_core.domain.transport_referral_utils import (
                    promote_facility_to_lab,
                )

                facility = new_state.facilities[new_lab_id]
                new_state = promote_facility_to_lab(new_state, facility)
            
        return new_state
    
    def with_new_lab(self, facility: Facility) -> "NetworkState":
        """Return new state with facility converted to lab (structural move only).

        For production HF→lab conversion use promote_facility_to_lab() which also
        drops stale outbound referrals.
        """
        new_state = self.copy()
        # Remove from facilities if present
        if facility.id in new_state.facilities:
            del new_state.facilities[facility.id]
        # Add to labs
        facility.is_lab = True
        new_state.labs[facility.id] = facility
        return new_state
    
    def extract_subgraph(
        self, 
        entity_ids: List[str],
        include_referring_facilities: bool = True,
        include_district_labs: bool = True
    ) -> "NetworkState":
        """
        Extract subgraph containing specified entities and their relevant context.
        
        Args:
            entity_ids: List of facility/lab IDs to start from
            include_referring_facilities: Whether to recursively include facilities referring to target labs
            include_district_labs: Whether to include all other labs in the same district (for redesign candidates)
            
        Returns:
            New NetworkState with filtered entities
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Start with requested entities
        entity_set = set(entity_ids)
        initial_count = len(entity_set)
        
        # 1. Expand to include referring facilities (sources of demand)
        if include_referring_facilities:
            referring_facilities = set()
            for ref in self.referrals:
                if ref.lab_id in entity_set:
                    referring_facilities.add(ref.facility_id)
            
            if referring_facilities:
                entity_set.update(referring_facilities)
                logger.info(f"Expanded subgraph with {len(referring_facilities)} referring facilities")
        
        # 2. Expand to include other labs in same district (candidates for actions)
        if include_district_labs:
            # Find districts of currently selected entities
            target_districts = set()
            for entity_id in entity_set:
                entity = self.labs.get(entity_id) or self.facilities.get(entity_id)
                if entity and entity.admin_area_2:
                    target_districts.add(entity.admin_area_2)
            
            if target_districts:
                district_labs = set()
                for lab_id, lab in self.labs.items():
                    if lab.admin_area_2 in target_districts:
                        district_labs.add(lab_id)
                
                new_labs = district_labs - entity_set
                if new_labs:
                    entity_set.update(new_labs)
                    logger.info(f"Expanded subgraph with {len(new_labs)} candidate labs from districts: {target_districts}")

        logger.info(f"Extracting subgraph for {len(entity_set)} entities (initial: {initial_count})")
        
        # Filter labs
        filtered_labs = {
            lab_id: lab for lab_id, lab in self.labs.items()
            if lab_id in entity_set
        }
        
        # Filter facilities
        filtered_facilities = {
            fac_id: fac for fac_id, fac in self.facilities.items()
            if fac_id in entity_set
        }
        
        # Collect all entity IDs (labs + facilities)
        all_entity_ids = set(filtered_labs.keys()) | set(filtered_facilities.keys())
        
        # Filter devices (only devices at included labs)
        filtered_devices = {
            dev_id: dev for dev_id, dev in self.devices.items()
            if dev.facility_id in all_entity_ids
        }
        
        # Filter referrals (only referrals involving included entities)
        filtered_referrals = [
            ref for ref in self.referrals
            if ref.facility_id in all_entity_ids or ref.lab_id in all_entity_ids
        ]
        
        # Filter transport routes (only routes between included entities)
        filtered_routes = [
            route for route in self.transport_routes
            if route.origin_facility_id in all_entity_ids 
            and route.dest_facility_id in all_entity_ids
        ]
        
        # Filter test volumes (only for included entities)
        filtered_volumes = {
            fac_id: volume for fac_id, volume in self.test_volumes.items()
            if fac_id in all_entity_ids
        }

        filtered_volumes_by_type = None
        if self.test_volumes_by_type:
            filtered_volumes_by_type = {
                fid: dict(by_type)
                for fid, by_type in self.test_volumes_by_type.items()
                if fid in all_entity_ids
            }
        
        logger.info(f"Subgraph: {len(filtered_labs)} labs, {len(filtered_facilities)} facilities, "
                   f"{len(filtered_devices)} devices, {len(filtered_referrals)} referrals, "
                   f"{len(filtered_routes)} routes")
        
        filtered_type_vols = None
        if self.device_type_volumes:
            filtered_type_vols = {
                k: v for k, v in self.device_type_volumes.items()
                if k[0] in all_entity_ids
            }

        filtered_caps = None
        if self.facility_test_capabilities:
            filtered_caps = {
                (lid, tid)
                for lid, tid in self.facility_test_capabilities
                if lid in all_entity_ids
            }

        filtered_device_active = None
        if self.device_active_tests:
            filtered_device_active = {
                did: set(tests)
                for did, tests in self.device_active_tests.items()
                if did in filtered_devices
            }
        
        filtered_type_test_vols = None
        if self.device_type_test_volumes:
            filtered_type_test_vols = {
                k: v
                for k, v in self.device_type_test_volumes.items()
                if k[0] in all_entity_ids
            }

        # Create new NetworkState with filtered data
        return NetworkState(
            project_id=self.project_id,
            facilities=filtered_facilities,
            labs=filtered_labs,
            devices=filtered_devices,
            referrals=filtered_referrals,
            transport_routes=filtered_routes,
            test_volumes=filtered_volumes,
            test_volumes_by_type=filtered_volumes_by_type,
            device_test_parameters_by_type=self.device_test_parameters_by_type,
            facility_test_capabilities=filtered_caps,
            device_active_tests=filtered_device_active,
            config=self.config,  # Share config
            device_types_info=self.device_types_info,
            scenario_mad_device_caps=self.scenario_mad_device_caps,
            device_type_volumes=filtered_type_vols,
            device_type_test_volumes=filtered_type_test_vols,
        )
    
    def with_referral_changed(
        self,
        facility_id: str,
        new_lab_id: str
    ) -> "NetworkState":
        """
        Return new state with referral assignment changed.
        
        Updates or creates referral for facility_id → new_lab_id.
        Ensures exactly one referral row per facility (merges duplicate rows).
        Does NOT modify original state.
        """
        new_state = self.copy()
        
        # Baseline data may contain multiple (facility, lab) rows — collapse to one target.
        matching_indices = [
            i for i, ref in enumerate(new_state.referrals)
            if ref.facility_id == facility_id
        ]
        
        if matching_indices:
            first_idx = matching_indices[0]
            ref = new_state.referrals[first_idx]
            merged_referral_count = sum(
                int(getattr(new_state.referrals[i], "referral_count", 0) or 0)
                for i in matching_indices
            )
            if merged_referral_count == 0:
                from dno_core.domain.transport_referral_utils import origin_demand_fallback
                merged_referral_count = origin_demand_fallback(
                    new_state.test_volumes, facility_id
                )

            distance_km = ref.distance_km
            if ref.lab_id != new_lab_id:
                facility = new_state.facilities.get(facility_id)
                lab = new_state.labs.get(new_lab_id)
                if facility and lab and facility.has_coordinates and lab.has_coordinates:
                    from dno_core.calculators.distance import DistanceCalculator
                    # Lab changed — store **geodesic** km (same convention as DB routes / referrals).
                    # Sample transport cost applies circuity (×1.4) in CostCalculator only.
                    distance_km = DistanceCalculator.facility_geodesic_km(facility, lab) or 0.0

            # Update the kept row with the new lab assignment.
            if isinstance(ref, Referral):
                # Create new Referral with updated lab_id and distance
                new_ref = Referral(
                    facility_id=ref.facility_id,
                    lab_id=new_lab_id,
                    referral_count=merged_referral_count,
                    distance_km=distance_km,
                    is_synthetic=ref.is_synthetic,
                    confidence=ref.confidence,
                )
            else:  # SyntheticReferral
                from dno_core.models.referral import SyntheticReferral
                new_ref = SyntheticReferral(
                    facility_id=ref.facility_id,
                    lab_id=new_lab_id,
                    distance_km=distance_km,
                    is_synthetic=ref.is_synthetic,
                    confidence=ref.confidence,
                )
            new_state.referrals[first_idx] = new_ref
            # Drop duplicate rows so demand is not double-counted against multiple labs.
            for i in sorted(matching_indices[1:], reverse=True):
                del new_state.referrals[i]
        else:
            # If no existing referral, create new one
            facility = new_state.facilities.get(facility_id)
            lab = new_state.labs.get(new_lab_id)
            
            distance_km = 0.0
            if facility and lab and facility.has_coordinates and lab.has_coordinates:
                from dno_core.calculators.distance import DistanceCalculator
                distance_km = DistanceCalculator.facility_geodesic_km(facility, lab) or 0.0
            
            from dno_core.domain.transport_referral_utils import origin_demand_fallback
            new_ref = Referral(
                facility_id=facility_id,
                lab_id=new_lab_id,
                referral_count=origin_demand_fallback(new_state.test_volumes, facility_id),
                distance_km=distance_km,
                is_synthetic=True,  # Created during optimization
                confidence=0.5,
            )
            new_state.referrals.append(new_ref)
        
        # Invalidate referral cache
        new_state._referrals_by_facility = None
        return new_state
    
    # ──────────────────────────────────────────────────────────────────────────
    # Serialization
    # ──────────────────────────────────────────────────────────────────────────
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "project_id": self.project_id,
            "facility_count": len(self.facilities),
            "lab_count": len(self.labs),
            "device_count": len(self.devices),
            "referral_count": len(self.referrals),
            "total_capacity": self.total_network_capacity(),
            "total_demand": self.total_network_demand(),
        }
    
    def to_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        working_days = self.config.working_days_per_year if self.config else 300
        
        return {
            "project_id": self.project_id,
            "facilities": len(self.facilities),
            "labs": len(self.labs),
            "devices": len(self.devices),
            "referrals": len(self.referrals),
            "synthetic_referrals": sum(1 for r in self.referrals if r.is_synthetic),
            "total_capacity": self.total_network_capacity(working_days),
            "total_demand": self.total_network_demand(),
            "overall_utilization": (
                self.total_network_demand() / self.total_network_capacity(working_days)
                if self.total_network_capacity(working_days) > 0 else 0.0
            ),
        }
