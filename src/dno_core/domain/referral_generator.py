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
Synthetic referral generator for when historical data is missing.
"""

from typing import List, Dict, Optional, Set
import logging
import math

from dno_core.models.facility import Facility
from dno_core.models.referral import SyntheticReferral
from dno_core.models.config import EngineConfig
from dno_core.models.device import DeviceInventory
from dno_core.models.network_state import NetworkState
from dno_core.domain.transport_referral_utils import is_lab_referrable, reconcile_referrals

logger = logging.getLogger(__name__)


def backfill_unassigned_referrals(
    state: NetworkState,
    *,
    only_facility_ids: Optional[Set[str]] = None,
) -> NetworkState:
    """
    Append synthetic referrals for demand HFs with no outbound referral yet.

    Baseline ingest routes are untouched. When only_facility_ids is set (scenario
    loader), backfill is limited to scenario CREATE HFs — avoids re-routing legacy
    leakage or undoing user DELETE referral patches on old sites.
    """
    state = reconcile_referrals(state)
    if state.config is None:
        return state
    if not state.labs:
        logger.warning("backfill_unassigned_referrals: no labs in state — skipping")
        return state

    assigned = {r.facility_id for r in state.referrals}
    unassigned: List[Facility] = []
    for f in state.facilities.values():
        if f.is_lab or f.id in assigned:
            continue
        if only_facility_ids is not None and f.id not in only_facility_ids:
            continue
        if not f.has_coordinates:
            continue
        if (state.test_volumes.get(f.id, 0) or 0) <= 0:
            continue
        unassigned.append(f)

    if not unassigned:
        return state

    gen = SyntheticReferralGenerator(state.config)
    referrable_labs = [
        lab for lab in state.labs.values() if is_lab_referrable(state, lab.id)
    ]
    new_refs = gen.generate_referrals(
        unassigned,
        referrable_labs,
        state.devices,
        max_referrals_per_facility=1,
    )
    if not new_refs:
        return state

    state.referrals.extend(new_refs)
    state._referrals_by_facility = None
    logger.info(
        "Backfilled %d synthetic referrals for %d unassigned facilities",
        len(new_refs),
        len(unassigned),
    )
    return state


class SyntheticReferralGenerator:
    """
    Generate referral linkages when historical data is missing.
    
    Used when:
    - Historical referrals table is empty or incomplete
    - Specific facilities have no referral history
    - New facilities added without historical data
    
    Generation algorithm considers:
    1. Distance (nearest lab preferred)
    2. Capacity availability at lab
    3. Administrative boundary alignment
    4. Facility tier compatibility
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize referral generator.
        
        Args:
            config: Engine configuration with referral weights
        """
        self.config = config
        
        # Weights for scoring
        self.distance_weight = float(config.referral_distance_weight)
        self.capacity_weight = float(config.referral_capacity_weight)
        self.admin_weight = float(config.referral_admin_weight)
        self.tier_weight = float(config.referral_tier_weight)
    
    def generate_referrals(
        self,
        facilities: List[Facility],
        labs: List[Facility],
        devices: Optional[Dict[str, DeviceInventory]] = None,
        max_referrals_per_facility: Optional[int] = None
    ) -> List[SyntheticReferral]:
        """
        Generate referral linkages based on scoring algorithm.
        
        Args:
            facilities: List of non-lab facilities
            labs: List of labs
            devices: Optional device inventory for capacity calculation
            max_referrals_per_facility: Max labs per facility (default from config)
            
        Returns:
            List of SyntheticReferral instances
        """
        if max_referrals_per_facility is None:
            max_referrals_per_facility = self.config.max_referrals_per_facility
        
        synthetic_referrals: List[SyntheticReferral] = []
        
        for facility in facilities:
            if facility.is_lab:
                continue  # Labs don't refer out
            
            if not facility.has_coordinates:
                logger.warning(f"Facility {facility.id} has no coordinates, skipping")
                continue
            
            # Find and score candidate labs
            scored_candidates = []
            for lab in labs:
                if not lab.has_coordinates:
                    continue
                
                scores = self._calculate_scores(facility, lab, devices)
                total_score = self._weighted_score(scores)
                
                scored_candidates.append((lab, scores, total_score))
            
            # Sort by score descending
            scored_candidates.sort(key=lambda x: x[2], reverse=True)
            
            # Take top N
            for lab, scores, total_score in scored_candidates[:max_referrals_per_facility]:
                distance = self._calculate_distance(facility, lab)
                crosses_boundary = not facility.is_in_same_admin_area(
                    lab, 
                    self.config.admin_boundary_level
                )
                
                referral = SyntheticReferral(
                    facility_id=facility.id,
                    lab_id=lab.id,
                    distance_km=distance,
                    confidence=total_score,
                    is_synthetic=True,
                    generation_method="distance_capacity_admin_tier",
                    admin_boundary_crossed=crosses_boundary,
                    distance_score=scores["distance"],
                    capacity_score=scores["capacity"],
                    admin_score=scores["admin"],
                    tier_score=scores["tier"],
                )
                
                synthetic_referrals.append(referral)
        
        logger.info(
            f"Generated {len(synthetic_referrals)} synthetic referrals "
            f"for {len(facilities)} facilities"
        )
        
        return synthetic_referrals
    
    def _calculate_scores(
        self,
        facility: Facility,
        lab: Facility,
        devices: Optional[Dict[str, DeviceInventory]] = None
    ) -> Dict[str, float]:
        """
        Calculate component scores for a facility-lab pair.
        
        Returns:
            Dict with distance, capacity, admin, tier scores (0-1 each)
        """
        return {
            "distance": self._score_distance(facility, lab),
            "capacity": self._score_capacity(lab, devices),
            "admin": self._score_admin_alignment(facility, lab),
            "tier": self._score_tier_compatibility(facility, lab),
        }
    
    def _weighted_score(self, scores: Dict[str, float]) -> float:
        """Calculate weighted total score."""
        total = (
            self.distance_weight * scores["distance"] +
            self.capacity_weight * scores["capacity"] +
            self.admin_weight * scores["admin"] +
            self.tier_weight * scores["tier"]
        )
        return max(0.0, min(1.0, total))
    
    def _score_distance(self, facility: Facility, lab: Facility) -> float:
        """
        Score based on distance (closer = higher).
        
        Uses exponential decay: score = e^(-distance/50)
        At 50km, score ≈ 0.37
        At 100km, score ≈ 0.14
        """
        distance = self._calculate_distance(facility, lab)
        if distance <= 0:
            return 1.0
        
        # Exponential decay with 50km half-life
        score = math.exp(-distance / 50)
        return max(0.0, min(1.0, score))
    
    def _score_capacity(
        self, 
        lab: Facility, 
        devices: Optional[Dict[str, DeviceInventory]] = None
    ) -> float:
        """
        Score based on capacity availability.
        
        Without device data, use a neutral score of 0.5.
        With device data, score based on available capacity.
        """
        if devices is None:
            return 0.5  # Neutral score without data
        
        # Calculate lab capacity from devices
        lab_devices = [d for d in devices.values() if d.facility_id == lab.id]
        if not lab_devices:
            return 0.3  # Low score if no devices
        
        # Score based on average utilization (lower util = more capacity)
        avg_util = sum(d.current_utilization for d in lab_devices) / len(lab_devices)
        available_capacity_ratio = 1.0 - avg_util
        
        return max(0.0, min(1.0, available_capacity_ratio))
    
    def _score_admin_alignment(self, facility: Facility, lab: Facility) -> float:
        """
        Score based on admin boundary alignment.
        
        Same admin_area_2: 1.0
        Same admin_area_1 only: 0.5
        Different admin_area_1: 0.2
        """
        if facility.admin_area_2 == lab.admin_area_2:
            return 1.0
        if facility.admin_area_1 == lab.admin_area_1:
            return 0.5
        return 0.2
    
    def _score_tier_compatibility(self, facility: Facility, lab: Facility) -> float:
        """
        Score based on tier compatibility.
        
        Higher tier facilities should prefer higher tier labs.
        """
        facility_tier = facility.tier_level
        lab_tier = lab.tier_level
        
        if lab_tier >= facility_tier:
            return 1.0
        
        # Penalize if lab tier is lower than facility tier
        tier_gap = facility_tier - lab_tier
        return max(0.0, 1.0 - (tier_gap * 0.2))
    
    def _calculate_distance(self, f1: Facility, f2: Facility) -> float:
        """
        Calculate Haversine distance between two facilities in km.
        """
        if not f1.has_coordinates or not f2.has_coordinates:
            return float('inf')
        
        lat1 = float(f1.latitude)
        lon1 = float(f1.longitude)
        lat2 = float(f2.latitude)
        lon2 = float(f2.longitude)
        
        return self._haversine_distance(lat1, lon1, lat2, lon2)
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate Haversine distance in km."""
        R = 6371  # Earth radius in km
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (
            math.sin(delta_lat / 2) ** 2 +
            math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
