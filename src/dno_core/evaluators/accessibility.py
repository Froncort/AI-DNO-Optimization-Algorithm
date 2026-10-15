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
Accessibility evaluator for geographic access analysis.
"""

from typing import Dict, List, Any
import logging

from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.facility import Facility
from dno_core.calculators.distance import DistanceCalculator

logger = logging.getLogger(__name__)


class AccessibilityEvaluator:
    """
    Evaluate geographic accessibility of diagnostic network.
    
    Metrics:
    - Facilities with/without lab access
    - Average distance to nearest lab
    - Population coverage by distance bands
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize accessibility evaluator.
        
        Args:
            config: Engine configuration
        """
        self.config = config
        self.baseline_score: float | None = None
    
    def evaluate(self, state: NetworkState) -> Dict:
        """
        Evaluate network accessibility.
        
        Returns:
            Dict with accessibility metrics including population-weighted score
        """
        facilities = list(state.facilities.values())
        labs = list(state.labs.values())
        
        # Find nearest lab for each facility
        facility_access: Dict[str, Dict] = {}
        distances: List[float] = []
        facilities_without_access: List[str] = []
        
        # Facility-count-based accessibility calculation
        # (Fallback to population if available)
        total_weight = 0.0
        weighted_accessibility_sum = 0.0
        # Build referral map: facility_id -> assigned_lab_id
        referral_map = {}
        for ref in state.referrals:
            referral_map[ref.facility_id] = ref.lab_id
        
        # Create lab lookup
        lab_lookup = {lab.id: lab for lab in labs}
        
        # Calculate accessibility for each facility
        for facility in facilities:
            # Skip labs themselves
            if facility.is_lab:
                continue
            
            # Facilities must have coordinates to be evaluated
            if not facility.has_coordinates:
                facilities_without_access.append(facility.id)
                continue
            
            # Get assigned lab from referral network
            assigned_lab_id = referral_map.get(facility.id)
            if not assigned_lab_id or assigned_lab_id not in lab_lookup:
                # No referral or lab not found - try nearest as fallback
                nearest_lab = None
                min_distance = float('inf')
                
                for lab in labs:
                    distance = DistanceCalculator.facility_distance(facility, lab)
                    if distance is not None and distance < min_distance:
                        min_distance = distance
                        nearest_lab = lab
                
                if nearest_lab:
                    assigned_lab_id = nearest_lab.id
                else:
                    facilities_without_access.append(facility.id)
                    continue
            
            # Calculate distance to assigned lab
            assigned_lab = lab_lookup[assigned_lab_id]
            distance = DistanceCalculator.facility_distance(facility, assigned_lab)
            
            if distance is not None:
                facility_access[facility.id] = {
                    "assigned_lab_id": assigned_lab_id,
                    "distance_km": distance,
                    "distance_band": DistanceCalculator.get_distance_band(distance),
                }
                distances.append(distance)
                
                # Calculate accessibility score for this facility
                # Score decreases with distance: 1 / (1 + distance^2)
                accessibility_score = 1.0 / (1 + (distance ** 2))
                
                # Weight by population if available, otherwise equal weighting (1.0)
                population = facility.population or 0
                weight = population if population > 0 else 1.0
                
                weighted_accessibility_sum += accessibility_score * weight
                total_weight += weight
            else:
                facilities_without_access.append(facility.id)
        
        # Calculate overall accessibility score
        pop_weighted_accessibility = (weighted_accessibility_sum / total_weight) if total_weight > 0 else 0.0
        
        # Calculate statistics FIRST (before logging)
        avg_distance = sum(distances) / len(distances) if distances else 0
        
        # Store baseline on first call
        if self.baseline_score is None:
            self.baseline_score = pop_weighted_accessibility
            weighting_type = "population" if any(f.population and f.population > 0 for f in facilities) else "facility count"
            logger.info(f"Baseline accessibility score: {self.baseline_score:.6f} (using {weighting_type} weighting)")
        
        # Calculate improvement from baseline
        improvement_pct = 0.0
        if self.baseline_score and self.baseline_score > 0:
            improvement_pct = ((pop_weighted_accessibility - self.baseline_score) / self.baseline_score) * 100
            
            # Log accessibility change details
            if abs(improvement_pct) > 0.001:
                logger.info(f"📊 Accessibility: {self.baseline_score:.6f} → {pop_weighted_accessibility:.6f} ({improvement_pct:+.3f}%)")
                logger.info(f"   Avg distance: {avg_distance:.2f}km, facilities evaluated: {len(distances)}")
        max_distance = max(distances) if distances else 0
        
        # Count by distance band
        band_counts = {
            "0-10": 0,
            "10-25": 0,
            "25-50": 0,
            "50-100": 0,
            "100+": 0,
        }
        for d in distances:
            band = DistanceCalculator.get_distance_band(d)
            band_counts[band] += 1
        
        # NEW: Calculate explainable percentage metrics
        total_with_access = len(distances)
        
        # 1. Facilities within distance bands (percentage)
        facilities_within_10km = sum(1 for d in distances if d <= 10)
        facilities_within_25km = sum(1 for d in distances if d <= 25)
        facilities_within_50km = sum(1 for d in distances if d <= 50)
        
        pct_within_10km = (facilities_within_10km / total_with_access * 100) if total_with_access > 0 else 0
        pct_within_25km = (facilities_within_25km / total_with_access * 100) if total_with_access > 0 else 0
        pct_within_50km = (facilities_within_50km / total_with_access * 100) if total_with_access > 0 else 0
        
        # Calculate ASD-based accessibility score (0-100)
        # Score based on: (1) Average distance and (2) % facilities within reasonable distance
        # Reference shows "ASD 16km, 29% beyond ASD" as good accessibility
        
        # Component 1: Distance score (0-100, lower distance = higher score)
        # 0km = 100, 50km = 50, 100km+ = 0
        distance_score = max(0, 100 - (avg_distance * 1.0))  # Linear: -1 point per km
        
        # Component 2: Coverage score based on % within reasonable bands
        # Weight: 50% within 25km, 30% within 50km, 20% within 10km
        coverage_score = (
            pct_within_10km * 0.20 +
            pct_within_25km * 0.50 +
            pct_within_50km * 0.30
        )
        
        # Combined accessibility score (70% distance, 30% coverage)
        accessibility_score_asd = (distance_score * 0.70) + (coverage_score * 0.30)
        
        return {
            "total_facilities": len(facilities),
            "facilities_with_access": len(facility_access),
            "facilities_without_access": len(facilities_without_access),
            "average_distance_km": round(avg_distance, 2),
            "max_distance_km": round(max_distance, 2),
            "distance_band_distribution": band_counts,
            "facility_access_details": facility_access,
            # Population-weighted metrics (original 0-1 scale) - DEPRECATED
            "population_weighted_accessibility": pop_weighted_accessibility,
            "baseline_accessibility": self.baseline_score or 0,
            "accessibility_improvement_pct": improvement_pct,
            # NEW: ASD-based accessibility score (0-100, higher is better)
            "accessibility_score": round(accessibility_score_asd, 1),
            "avg_distance_km": round(avg_distance, 2),
            "pct_within_10km": round(pct_within_10km, 1),
            "pct_within_25km": round(pct_within_25km, 1),
            "pct_within_50km": round(pct_within_50km, 1),
        }
    
    def get_underserved_facilities(
        self, 
        state: NetworkState, 
        max_distance_km: float = 50.0
    ) -> List[str]:
        """
        Get facilities beyond acceptable distance from nearest lab.
        """
        facilities = list(state.facilities.values())
        labs = list(state.labs.values())
        underserved = []
        
        for facility in facilities:
            if not facility.has_coordinates:
                underserved.append(facility.id)
                continue
            
            # Find nearest lab
            min_distance = float('inf')
            for lab in labs:
                distance = DistanceCalculator.facility_distance(facility, lab)
                if distance is not None:
                    min_distance = min(min_distance, distance)
            
            if min_distance > max_distance_km:
                underserved.append(facility.id)
        
        return underserved
    
    def calculate_improvement(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> float:
        """
        Calculate accessibility improvement between states.
        
        Returns improvement as percentage (positive = better).
        Uses population-weighted accessibility score.
        """
        # Evaluate baseline (sets self.baseline_score)
        baseline_eval = self.evaluate(baseline_state)
        
        # Evaluate optimized (compares against baseline)
        optimized_eval = self.evaluate(optimized_state)
        
        # Return the calculated improvement percentage
        return optimized_eval["accessibility_improvement_pct"]
    
    def explain_accessibility(self, state: NetworkState, baseline_state: NetworkState = None) -> str:
        """
        Generate human-readable explanation of accessibility metrics.
        
        Args:
            state: Current network state to explain
            baseline_state: Optional baseline for comparison
            
        Returns:
            Human-readable explanation string
        """
        eval_result = self.evaluate(state)
        
        total_facilities = eval_result["total_facilities"]
        with_access = eval_result["facilities_with_access"]
        without_access = eval_result["facilities_without_access"]
        avg_distance = eval_result["average_distance_km"]
        max_distance = eval_result["max_distance_km"]
        bands = eval_result["distance_band_distribution"]
        accessibility_score = eval_result["population_weighted_accessibility"]
        improvement_pct = eval_result["accessibility_improvement_pct"]
        
        explanation = f"""
Network Accessibility Analysis

Overall Score: {accessibility_score:.4f}
  (Population-weighted accessibility: 1.0 = perfect, 0.0 = no access)
  Formula: Σ(population × 1/(1+distance²)) / Σ(population)

Facility Coverage:
  • Total Facilities: {total_facilities}
  • With Lab Access: {with_access} ({with_access/total_facilities*100:.1f}%)
  • Without Access: {without_access}

Distance Metrics:
  • Average Distance to Lab: {avg_distance:.1f} km
  • Maximum Distance: {max_distance:.1f} km
"""
        
        # Add distance band distribution
        explanation += "\nDistance Band Distribution:\n"
        for band, count in sorted(bands.items()):
            pct = (count / with_access * 100) if with_access > 0 else 0
            explanation += f"  • {band} km: {count} facilities ({pct:.1f}%)\n"
        
        # Add improvement info if baseline exists
        if baseline_state is not None and improvement_pct != 0:
            direction = "improved" if improvement_pct > 0 else "worsened"
            explanation += f"\nChange from Baseline:\n"
            explanation += f"  {direction.upper()}: {abs(improvement_pct):.1f}% {direction}\n"
            
            # Identify facilities that improved/worsened
            baseline_eval = self.evaluate(baseline_state)
            baseline_access = baseline_eval["facility_access_details"]
            current_access = eval_result["facility_access_details"]
            
            improved = []
            worsened = []
            
            for fac_id in current_access:
                if fac_id in baseline_access:
                    baseline_dist = baseline_access[fac_id]["distance_km"]
                    current_dist = current_access[fac_id]["distance_km"]
                    
                    if current_dist < baseline_dist:
                        facility = state.facilities.get(fac_id)
                        fac_name = facility.name if facility else fac_id[:8]
                        improved.append({
                            "name": fac_name,
                            "baseline_dist": baseline_dist,
                            "current_dist": current_dist,
                            "improvement": baseline_dist - current_dist
                        })
                    elif current_dist > baseline_dist:
                        facility = state.facilities.get(fac_id)
                        fac_name = facility.name if facility else fac_id[:8]
                        worsened.append({
                            "name": fac_name,
                            "baseline_dist": baseline_dist,
                            "current_dist": current_dist,
                            "degradation": current_dist - baseline_dist
                        })
            
            if improved:
                improved.sort(key=lambda x: x["improvement"], reverse=True)
                explanation += "\nTop Facilities with Improved Access:\n"
                for i, fac in enumerate(improved[:5], 1):
                    explanation += f"  {i}. {fac['name']}: {fac['baseline_dist']:.1f}km → {fac['current_dist']:.1f}km (−{fac['improvement']:.1f}km)\n"
            
            if worsened:
                worsened.sort(key=lambda x: x["degradation"], reverse=True)
                explanation += "\nFacilities with Degraded Access:\n"
                for i, fac in enumerate(worsened[:5], 1):
                    explanation += f"  {i}. {fac['name']}: {fac['baseline_dist']:.1f}km → {fac['current_dist']:.1f}km (+{fac['degradation']:.1f}km)\n"
        
        return explanation.strip()
