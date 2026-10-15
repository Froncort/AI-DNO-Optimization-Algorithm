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
RRE (Referral Redesign Efficiency) Evaluator.

Measures the efficiency improvement in referral network redesign.
RRE = (Baseline Distance - Optimized Distance) / Baseline Distance

Higher RRE = more efficient referral network.
"""

from typing import Dict, List, Any
from decimal import Decimal
import logging

from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.calculators.distance import DistanceCalculator

logger = logging.getLogger(__name__)


class RREEvaluator:
    """
    Referral Redesign Efficiency evaluator.
    
    Tracks how much the referral network has been optimized
    in terms of total distance reduction.
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize RRE evaluator.
        
        Args:
            config: Engine configuration
        """
        self.config = config
    
    def calculate_rre(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> Dict:
        """
        Calculate RRE metrics comparing baseline to optimized state.
        
        Args:
            baseline_state: Network state before optimization
            optimized_state: Network state after optimization
            
        Returns:
            Dict with RRE metrics:
            - baseline_total_distance: Sum of all referral distances (km)
            - optimized_total_distance: After optimization (km)
            - rre_score: Efficiency improvement (0-1, higher is better)
            - distance_saved_km: Total distance saved
            - facilities_improved: Count with shorter distances
            - facilities_worsened: Count with longer distances
            - avg_distance_baseline: Average referral distance before (ASD baseline)
            - avg_distance_optimized: Average referral distance after (ASD optimized)
            - asd_baseline: Average Service Distance before optimization
            - asd_optimized: Average Service Distance after optimization
            - asd_improvement_km: ASD improvement in km
            - asd_improvement_percent: ASD improvement as percentage
        """
        baseline_distances = self._calculate_referral_distances(baseline_state)
        optimized_distances = self._calculate_referral_distances(optimized_state)
        
        baseline_total = sum(baseline_distances.values())
        optimized_total = sum(optimized_distances.values())
        
        # Calculate RRE score incorporating both total distance and ASD
        # RRE now considers:
        # 1. Total distance reduction (70% weight)
        # 2. Average Service Distance (ASD) improvement (30% weight)
        if baseline_total > 0:
            total_distance_improvement = (baseline_total - optimized_total) / baseline_total
            
            # Calculate ASD improvement component
            num_facilities = len(baseline_distances)
            asd_baseline_calc = baseline_total / num_facilities if num_facilities > 0 else 0
            asd_optimized_calc = optimized_total / num_facilities if num_facilities > 0 else 0
            
            if asd_baseline_calc > 0:
                asd_improvement = (asd_baseline_calc - asd_optimized_calc) / asd_baseline_calc
            else:
                asd_improvement = 0.0
            
            # Weighted combination: 70% total distance, 30% ASD
            raw_rre_score = (0.7 * total_distance_improvement) + (0.3 * asd_improvement)
            rre_score = max(0.0, raw_rre_score)
        else:
            rre_score = 0.0
        
        # Count improvements/worsening
        facilities_improved = 0
        facilities_worsened = 0
        
        for facility_id in baseline_distances:
            baseline_dist = baseline_distances.get(facility_id, 0)
            optimized_dist = optimized_distances.get(facility_id, 0)
            
            if optimized_dist < baseline_dist:
                facilities_improved += 1
            elif optimized_dist > baseline_dist:
                facilities_worsened += 1
        
        # Calculate averages (ASD - Average Service Distance)
        num_facilities = len(baseline_distances)
        asd_baseline = baseline_total / num_facilities if num_facilities > 0 else 0
        asd_optimized = optimized_total / num_facilities if num_facilities > 0 else 0
        
        # Calculate ASD improvement
        asd_improvement_km = asd_baseline - asd_optimized
        asd_improvement_percent = (asd_improvement_km / asd_baseline * 100) if asd_baseline > 0 else 0
        
        # OPTIDX PDF metric: % facilities beyond ASD (target: reduce this).
        # Legacy threshold: each scenario's own mean ASD.
        facilities_beyond_asd_baseline = sum(1 for d in baseline_distances.values() if d > asd_baseline)
        facilities_beyond_asd_optimized = sum(1 for d in optimized_distances.values() if d > asd_optimized)
        pct_beyond_asd_baseline = (facilities_beyond_asd_baseline / num_facilities * 100) if num_facilities > 0 else 0
        pct_beyond_asd_optimized = (facilities_beyond_asd_optimized / num_facilities * 100) if num_facilities > 0 else 0

        # Common baseline ASD threshold — same km cutoff for baseline and optimised rows.
        baseline_asd_threshold_km = asd_baseline
        facilities_beyond_baseline_asd_baseline = sum(
            1 for d in baseline_distances.values() if d > baseline_asd_threshold_km
        )
        facilities_beyond_baseline_asd_optimized = sum(
            1 for d in optimized_distances.values() if d > baseline_asd_threshold_km
        )
        pct_facilities_beyond_baseline_asd_baseline = (
            facilities_beyond_baseline_asd_baseline / num_facilities * 100
        ) if num_facilities > 0 else 0
        pct_facilities_beyond_baseline_asd_optimized = (
            facilities_beyond_baseline_asd_optimized / num_facilities * 100
        ) if num_facilities > 0 else 0
        
        return {
            "baseline_total_distance": baseline_total,
            "optimized_total_distance": optimized_total,
            "rre_score": rre_score,
            "distance_saved_km": baseline_total - optimized_total,
            "facilities_improved": facilities_improved,
            "facilities_worsened": facilities_worsened,
            "avg_distance_baseline": asd_baseline,
            "avg_distance_optimized": asd_optimized,
            "asd_baseline": asd_baseline,
            "asd_optimized": asd_optimized,
            "asd_improvement_km": asd_improvement_km,
            "asd_improvement_percent": asd_improvement_percent,
            "total_facilities": num_facilities,
            "pct_facilities_beyond_asd_baseline": pct_beyond_asd_baseline,
            "pct_facilities_beyond_asd_optimized": pct_beyond_asd_optimized,
            "facilities_beyond_asd_baseline": facilities_beyond_asd_baseline,
            "facilities_beyond_asd_optimized": facilities_beyond_asd_optimized,
            "baseline_asd_threshold_km": baseline_asd_threshold_km,
            "facilities_beyond_baseline_asd_baseline": facilities_beyond_baseline_asd_baseline,
            "facilities_beyond_baseline_asd_optimized": facilities_beyond_baseline_asd_optimized,
            "pct_facilities_beyond_baseline_asd_baseline": pct_facilities_beyond_baseline_asd_baseline,
            "pct_facilities_beyond_baseline_asd_optimized": pct_facilities_beyond_baseline_asd_optimized,
        }
    
    def calculate_rre_by_admin_area(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> Dict[str, Dict]:
        """
        Calculate RRE metrics separately for each admin_area_1.
        
        Returns:
            Dict mapping admin_area_1 → RRE metrics for that admin area
        """
        # Get all admin areas
        admin_areas = set()
        for facility in baseline_state.facilities.values():
            if facility.admin_area_1:
                admin_areas.add(facility.admin_area_1)
        
        results = {}
        
        for admin_area in sorted(admin_areas):
            # Filter to this admin area
            baseline_area_distances = {}
            optimized_area_distances = {}
            
            for referral in baseline_state.referrals:
                facility = baseline_state.facilities.get(referral.facility_id)
                lab = baseline_state.labs.get(referral.lab_id)
                
                if facility and lab and facility.admin_area_1 == admin_area:
                    distance = DistanceCalculator.facility_distance(facility, lab)
                    if distance is not None:
                        baseline_area_distances[referral.facility_id] = distance
            
            for referral in optimized_state.referrals:
                facility = optimized_state.facilities.get(referral.facility_id)
                lab = optimized_state.labs.get(referral.lab_id)
                
                if facility and lab and facility.admin_area_1 == admin_area:
                    distance = DistanceCalculator.facility_distance(facility, lab)
                    if distance is not None:
                        optimized_area_distances[referral.facility_id] = distance
            
            # Calculate metrics for this admin area
            baseline_total = sum(baseline_area_distances.values())
            optimized_total = sum(optimized_area_distances.values())
            num_facilities = len(baseline_area_distances)
            
            if baseline_total > 0 and num_facilities > 0:
                asd_baseline = baseline_total / num_facilities
                asd_optimized = optimized_total / num_facilities
                asd_improvement_km = asd_baseline - asd_optimized
                asd_improvement_percent = (asd_improvement_km / asd_baseline * 100) if asd_baseline > 0 else 0
                
                # RRE score (weighted: 70% total distance, 30% ASD)
                total_dist_improvement = (baseline_total - optimized_total) / baseline_total
                asd_improvement_ratio = asd_improvement_km / asd_baseline if asd_baseline > 0 else 0
                rre_score = max(0, (0.7 * total_dist_improvement) + (0.3 * asd_improvement_ratio))
                
                # Facility impact
                improved = sum(1 for fid in baseline_area_distances if optimized_area_distances.get(fid, 0) < baseline_area_distances[fid])
                worsened = sum(1 for fid in baseline_area_distances if optimized_area_distances.get(fid, 0) > baseline_area_distances[fid])
                unchanged = num_facilities - improved - worsened

                # Facilities beyond ASD — legacy uses each run's own mean; also expose baseline-threshold counts.
                facilities_beyond_asd_baseline = sum(1 for d in baseline_area_distances.values() if d > asd_baseline)
                facilities_beyond_asd_optimized = sum(1 for d in optimized_area_distances.values() if d > asd_optimized)
                pct_beyond_asd_baseline = (facilities_beyond_asd_baseline / num_facilities * 100) if num_facilities > 0 else 0
                pct_beyond_asd_optimized = (facilities_beyond_asd_optimized / num_facilities * 100) if num_facilities > 0 else 0
                baseline_asd_threshold_km = asd_baseline
                facilities_beyond_baseline_asd_baseline = sum(
                    1 for d in baseline_area_distances.values() if d > baseline_asd_threshold_km
                )
                facilities_beyond_baseline_asd_optimized = sum(
                    1 for d in optimized_area_distances.values() if d > baseline_asd_threshold_km
                )
                pct_beyond_baseline_asd_baseline = (
                    facilities_beyond_baseline_asd_baseline / num_facilities * 100
                ) if num_facilities > 0 else 0
                pct_beyond_baseline_asd_optimized = (
                    facilities_beyond_baseline_asd_optimized / num_facilities * 100
                ) if num_facilities > 0 else 0

                results[admin_area] = {
                    "admin_area": admin_area,
                    "baseline_total_distance": baseline_total,
                    "optimized_total_distance": optimized_total,
                    "distance_saved_km": baseline_total - optimized_total,
                    "asd_baseline": asd_baseline,
                    "asd_optimized": asd_optimized,
                    "asd_improvement_km": asd_improvement_km,
                    "asd_improvement_percent": asd_improvement_percent,
                    "rre_score": rre_score,
                    "facilities_count": num_facilities,
                    "facilities_improved": improved,
                    "facilities_worsened": worsened,
                    "facilities_unchanged": unchanged,
                    "facilities_beyond_asd_baseline": facilities_beyond_asd_baseline,
                    "facilities_beyond_asd_optimized": facilities_beyond_asd_optimized,
                    "pct_beyond_asd_baseline": pct_beyond_asd_baseline,
                    "pct_beyond_asd_optimized": pct_beyond_asd_optimized,
                    "baseline_asd_threshold_km": baseline_asd_threshold_km,
                    "facilities_beyond_baseline_asd_baseline": facilities_beyond_baseline_asd_baseline,
                    "facilities_beyond_baseline_asd_optimized": facilities_beyond_baseline_asd_optimized,
                    "pct_beyond_baseline_asd_baseline": pct_beyond_baseline_asd_baseline,
                    "pct_beyond_baseline_asd_optimized": pct_beyond_baseline_asd_optimized,
                }
            else:
                results[admin_area] = {
                    "admin_area": admin_area,
                    "baseline_total_distance": 0,
                    "optimized_total_distance": 0,
                    "distance_saved_km": 0,
                    "asd_baseline": 0,
                    "asd_optimized": 0,
                    "asd_improvement_km": 0,
                    "asd_improvement_percent": 0,
                    "rre_score": 0,
                    "facilities_count": 0,
                    "facilities_improved": 0,
                    "facilities_worsened": 0,
                    "facilities_unchanged": 0,
                    "facilities_beyond_asd_baseline": 0,
                    "facilities_beyond_asd_optimized": 0,
                    "pct_beyond_asd_baseline": 0,
                    "pct_beyond_asd_optimized": 0,
                    "baseline_asd_threshold_km": 0,
                    "facilities_beyond_baseline_asd_baseline": 0,
                    "facilities_beyond_baseline_asd_optimized": 0,
                    "pct_beyond_baseline_asd_baseline": 0,
                    "pct_beyond_baseline_asd_optimized": 0,
                }
        
        return results

    def calculate_rre_by_admin_area_2(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> Dict[str, Dict]:
        """
        Calculate RRE metrics separately for each admin_area_2 (referring facility boundary).

        Same logic as calculate_rre_by_admin_area, but partitions by facility.admin_area_2.
        """
        admin_areas = set()
        for facility in baseline_state.facilities.values():
            if facility.admin_area_2:
                admin_areas.add(facility.admin_area_2)
        for lab in baseline_state.labs.values():
            if lab.admin_area_2:
                admin_areas.add(lab.admin_area_2)

        results: Dict[str, Dict] = {}

        for admin_area in sorted(admin_areas):
            baseline_area_distances: Dict[str, float] = {}
            optimized_area_distances: Dict[str, float] = {}

            for referral in baseline_state.referrals:
                facility = baseline_state.facilities.get(referral.facility_id)
                lab = baseline_state.labs.get(referral.lab_id)

                if facility and lab and facility.admin_area_2 == admin_area:
                    distance = DistanceCalculator.facility_distance(facility, lab)
                    if distance is not None:
                        baseline_area_distances[referral.facility_id] = distance

            for referral in optimized_state.referrals:
                facility = optimized_state.facilities.get(referral.facility_id)
                lab = optimized_state.labs.get(referral.lab_id)

                if facility and lab and facility.admin_area_2 == admin_area:
                    distance = DistanceCalculator.facility_distance(facility, lab)
                    if distance is not None:
                        optimized_area_distances[referral.facility_id] = distance

            baseline_total = sum(baseline_area_distances.values())
            optimized_total = sum(optimized_area_distances.values())
            num_facilities = len(baseline_area_distances)

            if baseline_total > 0 and num_facilities > 0:
                asd_baseline = baseline_total / num_facilities
                asd_optimized = optimized_total / num_facilities
                asd_improvement_km = asd_baseline - asd_optimized
                asd_improvement_percent = (asd_improvement_km / asd_baseline * 100) if asd_baseline > 0 else 0

                total_dist_improvement = (baseline_total - optimized_total) / baseline_total
                asd_improvement_ratio = asd_improvement_km / asd_baseline if asd_baseline > 0 else 0
                rre_score = max(0, (0.7 * total_dist_improvement) + (0.3 * asd_improvement_ratio))

                improved = sum(
                    1 for fid in baseline_area_distances
                    if optimized_area_distances.get(fid, 0) < baseline_area_distances[fid]
                )
                worsened = sum(
                    1 for fid in baseline_area_distances
                    if optimized_area_distances.get(fid, 0) > baseline_area_distances[fid]
                )
                unchanged = num_facilities - improved - worsened

                facilities_beyond_asd_baseline = sum(
                    1 for d in baseline_area_distances.values() if d > asd_baseline
                )
                facilities_beyond_asd_optimized = sum(
                    1 for d in optimized_area_distances.values() if d > asd_optimized
                )
                pct_beyond_asd_baseline = (
                    facilities_beyond_asd_baseline / num_facilities * 100
                ) if num_facilities > 0 else 0
                pct_beyond_asd_optimized = (
                    facilities_beyond_asd_optimized / num_facilities * 100
                ) if num_facilities > 0 else 0
                baseline_asd_threshold_km = asd_baseline
                facilities_beyond_baseline_asd_baseline = sum(
                    1 for d in baseline_area_distances.values() if d > baseline_asd_threshold_km
                )
                facilities_beyond_baseline_asd_optimized = sum(
                    1 for d in optimized_area_distances.values() if d > baseline_asd_threshold_km
                )
                pct_beyond_baseline_asd_baseline = (
                    facilities_beyond_baseline_asd_baseline / num_facilities * 100
                ) if num_facilities > 0 else 0
                pct_beyond_baseline_asd_optimized = (
                    facilities_beyond_baseline_asd_optimized / num_facilities * 100
                ) if num_facilities > 0 else 0

                results[admin_area] = {
                    "admin_area_2": admin_area,
                    "baseline_total_distance": baseline_total,
                    "optimized_total_distance": optimized_total,
                    "distance_saved_km": baseline_total - optimized_total,
                    "asd_baseline": asd_baseline,
                    "asd_optimized": asd_optimized,
                    "asd_improvement_km": asd_improvement_km,
                    "asd_improvement_percent": asd_improvement_percent,
                    "rre_score": rre_score,
                    "facilities_count": num_facilities,
                    "facilities_improved": improved,
                    "facilities_worsened": worsened,
                    "facilities_unchanged": unchanged,
                    "facilities_beyond_asd_baseline": facilities_beyond_asd_baseline,
                    "facilities_beyond_asd_optimized": facilities_beyond_asd_optimized,
                    "pct_beyond_asd_baseline": pct_beyond_asd_baseline,
                    "pct_beyond_asd_optimized": pct_beyond_asd_optimized,
                    "baseline_asd_threshold_km": baseline_asd_threshold_km,
                    "facilities_beyond_baseline_asd_baseline": facilities_beyond_baseline_asd_baseline,
                    "facilities_beyond_baseline_asd_optimized": facilities_beyond_baseline_asd_optimized,
                    "pct_beyond_baseline_asd_baseline": pct_beyond_baseline_asd_baseline,
                    "pct_beyond_baseline_asd_optimized": pct_beyond_baseline_asd_optimized,
                }
            else:
                results[admin_area] = {
                    "admin_area_2": admin_area,
                    "baseline_total_distance": 0,
                    "optimized_total_distance": 0,
                    "distance_saved_km": 0,
                    "asd_baseline": 0,
                    "asd_optimized": 0,
                    "asd_improvement_km": 0,
                    "asd_improvement_percent": 0,
                    "rre_score": 0,
                    "facilities_count": 0,
                    "facilities_improved": 0,
                    "facilities_worsened": 0,
                    "facilities_unchanged": 0,
                    "facilities_beyond_asd_baseline": 0,
                    "facilities_beyond_asd_optimized": 0,
                    "pct_beyond_asd_baseline": 0,
                    "pct_beyond_asd_optimized": 0,
                    "baseline_asd_threshold_km": 0,
                    "facilities_beyond_baseline_asd_baseline": 0,
                    "facilities_beyond_baseline_asd_optimized": 0,
                    "pct_beyond_baseline_asd_baseline": 0,
                    "pct_beyond_baseline_asd_optimized": 0,
                }

        return results
    
    def _calculate_referral_distances(self, state: NetworkState) -> Dict[str, float]:
        """
        Calculate distance for each facility-to-lab referral.

        Also includes in-house labs (sites in state.labs that process their own demand
        with no outbound referral) at 0 km, so that the total_facilities count here
        is consistent with the Transport - HF Distance sheet row count.

        Returns:
            Dict mapping facility_id → distance to assigned lab (km)
        """
        distances: Dict[str, float] = {}

        for referral in state.referrals:
            # Prefer state.facilities; fall back to state.labs for sites that are
            # classified as labs but still appear as a referral origin.
            facility = state.facilities.get(referral.facility_id) or state.labs.get(referral.facility_id)
            lab = state.labs.get(referral.lab_id)

            if facility and lab:
                distance = DistanceCalculator.facility_distance(facility, lab)
                if distance is not None:
                    distances[str(referral.facility_id)] = distance

        # In-house labs have no referral row; include them at 0 km so the facility
        # count matches what build_referral_distance_comparison_rows now emits.
        for lab_fid, lab in state.labs.items():
            fid_str = str(lab_fid)
            if fid_str not in distances:
                vol = int(state.test_volumes.get(lab_fid, 0) or 0)
                if vol > 0:
                    distances[fid_str] = 0.0

        return distances
    
    def get_distance_distribution(self, state: NetworkState) -> Dict[str, int]:
        """
        Get distribution of referral distances by band.

        Returns:
            Counts keyed by ``onsite`` (<0.01 km), ``1-10``, ``10-25``, ``25-50``,
            ``50-100``, ``100+`` (km).
        """
        distances = self._calculate_referral_distances(state)
        
        distribution = {
            "onsite": 0,
            "1-10": 0,
            "10-25": 0,
            "25-50": 0,
            "50-100": 0,
            "100+": 0,
        }

        for distance in distances.values():
            if distance is None:
                continue
            # Same-site / co-located referrals vs 1–10 km (rest unchanged vs get_distance_band)
            if float(distance) < 0.01:
                distribution["onsite"] += 1
            elif float(distance) <= 10:
                distribution["1-10"] += 1
            elif float(distance) <= 25:
                distribution["10-25"] += 1
            elif float(distance) <= 50:
                distribution["25-50"] += 1
            elif float(distance) <= 100:
                distribution["50-100"] += 1
            else:
                distribution["100+"] += 1

        return distribution
    
    def calculate_improvement_by_facility(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> List[Dict]:
        """
        Calculate per-facility distance improvements.
        
        Returns:
            List of dicts with facility-level improvements
        """
        baseline_distances = self._calculate_referral_distances(baseline_state)
        optimized_distances = self._calculate_referral_distances(optimized_state)
        
        improvements = []
        
        for facility_id in baseline_distances:
            baseline_dist = baseline_distances.get(facility_id, 0)
            optimized_dist = optimized_distances.get(facility_id, 0)
            improvement = baseline_dist - optimized_dist
            
            if improvement != 0:  # Only include changed facilities
                facility = baseline_state.facilities.get(facility_id)
                improvements.append({
                    "facility_id": facility_id,
                    "facility_name": facility.name if facility else "Unknown",
                    "baseline_distance_km": baseline_dist,
                    "optimized_distance_km": optimized_dist,
                    "improvement_km": improvement,
                    "improvement_percent": (improvement / baseline_dist * 100) if baseline_dist > 0 else 0,
                })
        
        # Sort by improvement (largest first)
        improvements.sort(key=lambda x: x["improvement_km"], reverse=True)
        
        return improvements
    
    def explain_rre(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> str:
        """
        Generate human-readable explanation of RRE score.
        
        Args:
            baseline_state: Network before optimization
            optimized_state: Network after optimization
            
        Returns:
            Human-readable explanation string
        """
        rre_metrics = self.calculate_rre(baseline_state, optimized_state)
        improvements = self.calculate_improvement_by_facility(baseline_state, optimized_state)
        
        rre_score = rre_metrics["rre_score"]
        distance_saved = rre_metrics["distance_saved_km"]
        baseline_total = rre_metrics["baseline_total_distance"]
        optimized_total = rre_metrics["optimized_total_distance"]
        facilities_improved = rre_metrics["facilities_improved"]
        facilities_worsened = rre_metrics["facilities_worsened"]
        total_facilities = rre_metrics["total_facilities"]
        asd_baseline = rre_metrics["asd_baseline"]
        asd_optimized = rre_metrics["asd_optimized"]
        asd_improvement_km = rre_metrics["asd_improvement_km"]
        asd_improvement_percent = rre_metrics["asd_improvement_percent"]
        
        # Interpret score
        if rre_score > 0.2:
            interpretation = "EXCELLENT - Significant efficiency improvement"
        elif rre_score > 0.1:
            interpretation = "GOOD - Moderate efficiency improvement"
        elif rre_score > 0:
            interpretation = "MINOR - Small efficiency improvement"
        elif optimized_total > baseline_total:
            interpretation = "DEGRADED - Network efficiency worsened (Score floored at 0)"
        else:
            interpretation = "NO CHANGE - Network unchanged"
        
        explanation = f"""
Referral Redesign Efficiency (RRE) Analysis

RRE Score: {rre_score:.4f} ({rre_score*100:.1f}%)
  Interpretation: {interpretation}
  
  Formula: (Baseline Total Distance - Optimized Total Distance) / Baseline Total Distance
  Calculation: ({baseline_total:.1f}km - {optimized_total:.1f}km) / {baseline_total:.1f}km = {rre_score:.4f}

Distance Summary:
  • Baseline Total Distance: {baseline_total:.1f} km
  • Optimized Total Distance: {optimized_total:.1f} km
  • Distance Saved: {distance_saved:.1f} km ({abs(rre_score)*100:.1f}% reduction)

Average Service Distance (ASD):
  • ASD Baseline: {asd_baseline:.1f} km per facility
  • ASD Optimized: {asd_optimized:.1f} km per facility
  • ASD Improvement: {asd_improvement_km:.1f} km ({asd_improvement_percent:.1f}% {'reduction' if asd_improvement_km > 0 else 'increase'})

Facilities Beyond ASD (OPTIDX metric — uses each run's mean ASD as the threshold, not a fixed km cutoff):
  • Baseline: {rre_metrics.get('pct_facilities_beyond_asd_baseline', 0):.1f}% ({rre_metrics.get('facilities_beyond_asd_baseline', 0)} facilities)
  • Optimized: {rre_metrics.get('pct_facilities_beyond_asd_optimized', 0):.1f}% ({rre_metrics.get('facilities_beyond_asd_optimized', 0)} facilities)
  Note: If mean ASD drops after optimization, the threshold drops too — the share "above mean" can rise even when average distance improves.

Facility Impact:
  • Total Facilities: {total_facilities}
  • Improved: {facilities_improved} ({(facilities_improved/total_facilities*100) if total_facilities > 0 else 0:.1f}%)
  • Worsened: {facilities_worsened} ({(facilities_worsened/total_facilities*100) if total_facilities > 0 else 0:.1f}%)
  • Unchanged: {total_facilities - facilities_improved - facilities_worsened}
"""
        
        # Add top improvements
        if improvements:
            explanation += "\nTop Efficiency Improvements:\n"
            for i, imp in enumerate(improvements[:5], 1):
                if imp["improvement_km"] > 0:
                    explanation += f"  {i}. {imp['facility_name']}: "
                    explanation += f"{imp['baseline_distance_km']:.1f}km → {imp['optimized_distance_km']:.1f}km "
                    explanation += f"(-{imp['improvement_km']:.1f}km, -{imp['improvement_percent']:.1f}%)\n"
        
        # Add top degradations if any
        degradations = [imp for imp in improvements if imp["improvement_km"] < 0]
        if degradations:
            explanation += "\nNotable Degradations:\n"
            # Sort by degradation (worst first)
            degradations.sort(key=lambda x: x["improvement_km"])
            for i, imp in enumerate(degradations[:3], 1):
                explanation += f"  {i}. {imp['facility_name']}: "
                explanation += f"{imp['baseline_distance_km']:.1f}km → {imp['optimized_distance_km']:.1f}km "
                explanation += f"(+{abs(imp['improvement_km']):.1f}km, +{abs(imp['improvement_percent']):.1f}%)\n"
        
        # Add key contributing factors
        explanation += "\nKey Contributing Factors:\n"
        if rre_score > 0:
            explanation += "  • Referral network redesign successfully reduced travel distances\n"
            if facilities_improved > total_facilities * 0.5:
                explanation += f"  • Majority of facilities ({facilities_improved}) received closer lab assignments\n"
            if asd_improvement_km > 5:
                explanation += f"  • Average Service Distance (ASD) reduced by {asd_improvement_km:.1f}km per facility\n"
        else:
            explanation += "  • Optimization did not improve referral network efficiency\n"
            if facilities_worsened > facilities_improved:
                explanation += f"  ⚠️  More facilities worsened ({facilities_worsened}) than improved ({facilities_improved})\n"
            if asd_improvement_km < 0:
                explanation += f"  ⚠️  Average Service Distance (ASD) increased by {abs(asd_improvement_km):.1f}km per facility\n"
        
        return explanation.strip()
