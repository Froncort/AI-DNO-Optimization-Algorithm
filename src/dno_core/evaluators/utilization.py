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
Utilization evaluator for network utilization assessment.
"""

from typing import Dict, List, Any, Optional
import logging

from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.results import UtilizationReport
from dno_core.calculators.capacity import CapacityCalculator
from dno_core.calculators.shift_capacity import (
    ALL_TEST_BUCKET,
    CAPABILITY_GAP_UTIL,
    lab_total_demand_by_test,
    per_test_capacity_at_lab,
    rho_star_lab_utilization,
)

logger = logging.getLogger(__name__)


class UtilizationEvaluator:
    """
    Evaluate network utilization.
    
    Categorizes labs as:
    - Overutilized: > utilization_threshold_high (default 95%)
    - Underutilized: < utilization_threshold_low (default 40%)
    - Balanced: Within thresholds
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize utilization evaluator.
        
        Args:
            config: Engine configuration with thresholds
        """
        self.config = config
        self.threshold_low = float(config.utilization_threshold_low)
        self.threshold_high = float(config.utilization_threshold_high)
        self.capacity_calculator = CapacityCalculator(config)
    
    def evaluate(
        self, 
        state: NetworkState,
        threshold_high: Optional[float] = None,
        threshold_low: Optional[float] = None
    ) -> UtilizationReport:
        """
        Evaluate network utilization.
        
        Args:
            state: Current network state
            threshold_high: Optional override for high utilization threshold (default: config value)
            threshold_low: Optional override for low utilization threshold (default: config value)
            
        Returns:
            UtilizationReport with utilization breakdown
        """
        # Use provided thresholds or fall back to config
        use_threshold_high = threshold_high if threshold_high is not None else self.threshold_high
        use_threshold_low = threshold_low if threshold_low is not None else self.threshold_low
        
        if threshold_high is not None or threshold_low is not None:
            logger.info(f"Using custom thresholds: high={use_threshold_high:.1%}, low={use_threshold_low:.1%} (config: high={self.threshold_high:.1%}, low={self.threshold_low:.1%})")
        # Log utilization evaluation
        logger.debug(f"Utilization evaluate called: num_labs={len(state.labs)}, num_referrals={len(state.referrals)}, num_test_volumes={len(state.test_volumes)}")
        
        lab_utilizations: Dict[str, float] = {}
        lab_capacities: Dict[str, int] = {}
        lab_demands: Dict[str, int] = {}
        device_utilizations: Dict[str, float] = {}
        
        overutilized: List[str] = []
        underutilized: List[str] = []
        balanced: List[str] = []
        
        # Calculate capacity and demand per lab using the same method as frontend
        working_days = self.config.working_days_per_year if hasattr(self.config, 'working_days_per_year') else 300
        
        for lab_id, lab in state.labs.items():
            # Demand map is a pure function of state; compute once and reuse for ρ* below
            # so we don't scan referrals/volumes twice per lab.
            demand_map = lab_total_demand_by_test(state, lab_id)
            lab_demands[lab_id] = int(sum(demand_map.values()))

            # Per-device ρ* fill factor (shift-equivalent); single-test falls back to annual_capacity ratio
            utilization = rho_star_lab_utilization(
                state, lab_id, working_days, demand_by_test=demand_map
            )
            lab_utilizations[lab_id] = utilization

            cap_map = per_test_capacity_at_lab(state, lab_id, working_days)
            if cap_map:
                lab_capacities[lab_id] = int(sum(cap_map.values()))
            else:
                lab_devices = [d for d in state.devices.values() if d.facility_id == lab_id]
                lab_capacities[lab_id] = sum(
                    d.annual_capacity(working_days) for d in lab_devices
                )
        
        # Log lab utilizations calculated
        sample_labs = list(lab_utilizations.items())[:3]
        avg_utilization = sum(lab_utilizations.values()) / len(lab_utilizations) if lab_utilizations else 0
        logger.debug(f"Lab utilizations calculated: avg_utilization={avg_utilization:.2%}, sample_labs={[(lid[:8], f'{util:.2%}') for lid, util in sample_labs]}")
        labs_over_100 = []
        
        for lab_id, capacity in lab_capacities.items():
            utilization = lab_utilizations[lab_id]
            demand = lab_demands.get(lab_id, 0)
            
            # #region agent log - H4: Track labs over 100%
            if utilization > 1.0:
                lab = state.labs.get(lab_id)
                lab_name = lab.name if lab else lab_id[:8]
                labs_over_100.append({
                    "lab_id": lab_id,
                    "lab_name": lab_name,
                    "utilization": float(utilization),
                    "utilization_percent": float(utilization * 100),
                    "demand": int(demand),
                    "capacity": int(capacity)
                })
            # #endregion
            
            # Categorize using custom thresholds
            if utilization > use_threshold_high:
                overutilized.append(lab_id)
                logger.info(f"  [OVERUTILIZED] Lab {lab_id[:8]}: {utilization:.1%} (demand={demand}, capacity={capacity})")
            elif utilization < use_threshold_low:
                underutilized.append(lab_id)
            else:
                balanced.append(lab_id)
        
        # Log all labs with high utilization for debugging
        high_util_labs = [(lab_id, util) for lab_id, util in lab_utilizations.items() if util > 0.80]
        if high_util_labs:
            logger.info(f"Labs with >80% utilization: {len(high_util_labs)}")
            for lab_id, util in sorted(high_util_labs, key=lambda x: x[1], reverse=True)[:10]:
                cap = lab_capacities.get(lab_id, 0)
                dem = lab_demands.get(lab_id, 0)
                lab = state.labs.get(lab_id)
                lab_name = lab.name if lab else lab_id[:8]
                logger.info(f"    {lab_id[:8]} ({lab_name[:40]}): {util:.1%} (demand={dem:,}, capacity={cap:,}, threshold={use_threshold_high:.1%})")
        
        # Log ALL overutilized labs (above threshold) for debugging
        if overutilized:
            logger.info(f"=== OVERUTILIZED LABS DETECTED ({len(overutilized)} total, threshold={use_threshold_high:.1%}) ===")
            for lab_id in sorted(overutilized, key=lambda lid: lab_utilizations.get(lid, 0), reverse=True):
                util = lab_utilizations.get(lab_id, 0)
                cap = lab_capacities.get(lab_id, 0)
                dem = lab_demands.get(lab_id, 0)
                lab = state.labs.get(lab_id)
                lab_name = lab.name if lab else lab_id[:8]
                logger.info(f"  OVERUTILIZED: {lab_id[:8]} ({lab_name[:40]}): {util:.1%} (demand={dem:,}, capacity={cap:,})")
        else:
            highest_util = max(lab_utilizations.values()) * 100 if lab_utilizations else 0
            logger.info(f"No overutilized labs detected (threshold={use_threshold_high:.1%}). Highest utilization: {highest_util:.1f}%")
        
        # Calculate device-level utilization
        for device in state.devices.values():
            device_utilizations[device.device_id] = device.current_utilization
        
        # Calculate totals
        total_capacity = sum(lab_capacities.values())
        total_demand = sum(lab_demands.values())
        
        # Calculate overall utilization as weighted average of lab utilizations
        # This ensures it reflects changes when devices move between labs
        # Weight by capacity so labs with more capacity have more influence
        if total_capacity > 0:
            weighted_sum = sum(
                lab_utilizations.get(lab_id, 0) * lab_capacities.get(lab_id, 0)
                for lab_id in lab_capacities.keys()
            )
            overall_utilization = weighted_sum / total_capacity
        else:
            overall_utilization = 0.0
        
        report = UtilizationReport(
            total_labs=len(lab_capacities),
            total_devices=len(state.devices),
            total_capacity=total_capacity,
            total_demand=total_demand,
            overall_utilization=overall_utilization,
            overutilized_labs=overutilized,
            underutilized_labs=underutilized,
            balanced_labs=balanced,
            lab_utilizations=lab_utilizations,
            lab_capacities=lab_capacities,
            lab_demands=lab_demands,
            device_utilizations=device_utilizations,
        )
        
        logger.info(
            f"Utilization: {len(overutilized)} overutilized, "
            f"{len(underutilized)} underutilized, {len(balanced)} balanced"
        )
        
        return report
    
    def get_overutilized_labs(self, state: NetworkState) -> List[str]:
        """Get list of overutilized lab IDs."""
        report = self.evaluate(state)
        return report.overutilized_labs
    
    def get_underutilized_labs(self, state: NetworkState) -> List[str]:
        """Get list of underutilized lab IDs."""
        report = self.evaluate(state)
        return report.underutilized_labs
    
    def get_demand_gap_at_lab(
        self, 
        lab_id: str, 
        state: NetworkState
    ) -> int:
        """
        Calculate demand gap at an overutilized lab.
        
        Positive = demand exceeds capacity
        """
        capacity = self.capacity_calculator.lab_capacity(lab_id, state.devices)
        
        demand = sum(
            state.test_volumes.get(r.facility_id, 0)
            for r in state.referrals
            if r.lab_id == lab_id
        )
        
        return max(0, demand - capacity)
    
    def get_available_capacity_at_lab(
        self, 
        lab_id: str, 
        state: NetworkState
    ) -> int:
        """
        Calculate available capacity at an underutilized lab.
        """
        return self.capacity_calculator.available_capacity_at_lab(lab_id, state)
    
    def calculate_utilization_variance(self, state: NetworkState) -> float:
        """
        Calculate variance in lab utilizations.
        
        Lower variance = more balanced network.
        Higher variance = some labs overloaded, others idle.
        
        Returns:
            Variance of utilization rates across all labs
        """
        report = self.evaluate(state)
        utilizations = list(report.lab_utilizations.values())
        
        if not utilizations:
            return 0.0
        
        # Calculate mean
        mean = sum(utilizations) / len(utilizations)
        
        # Calculate variance
        variance = sum((u - mean) ** 2 for u in utilizations) / len(utilizations)
        
        return variance
    
    def calculate_utilization_std_dev(self, state: NetworkState) -> float:
        """
        Calculate standard deviation of lab utilizations.
        
        Returns:
            Standard deviation (more interpretable than variance)
        """
        variance = self.calculate_utilization_variance(state)
        return variance ** 0.5
    
    def identify_peak_bottlenecks(self, state: NetworkState) -> List[Dict]:
        """
        Identify labs that are bottlenecks (high utilization + high demand).
        
        Returns:
            List of bottleneck labs with details
        """
        report = self.evaluate(state)
        bottlenecks = []
        
        for lab_id in report.overutilized_labs:
            lab = state.labs.get(lab_id)
            if not lab:
                continue
            
            utilization = report.lab_utilizations.get(lab_id, 0)
            capacity = report.lab_capacities.get(lab_id, 0)
            demand = report.lab_demands.get(lab_id, 0)
            gap = demand - capacity
            
            bottlenecks.append({
                "lab_id": lab_id,
                "lab_name": lab.name,
                "utilization": utilization,
                "capacity": capacity,
                "demand": demand,
                "demand_gap": gap,
                "severity": "critical" if utilization > 1.2 else "high",
            })
        
        # Sort by demand gap (worst first)
        bottlenecks.sort(key=lambda x: x["demand_gap"], reverse=True)
        
        return bottlenecks
    
    def lab_utilization_breakdown(self, lab_id: str, state: NetworkState) -> Dict[str, Any]:
        """
        Get detailed breakdown of utilization for a specific lab.
        
        Returns per-facility demand contribution, capacity breakdown by device,
        and step-by-step utilization calculation.
        
        Args:
            lab_id: Lab ID to analyze
            state: Current network state
            
        Returns:
            Dict with breakdown details
        """
        lab = state.labs.get(lab_id)
        if not lab:
            return {"error": "Lab not found"}
        
        # Get lab capacity
        capacity = self.capacity_calculator.lab_capacity(lab_id, state.devices)
        
        # Get device breakdown
        lab_devices = [d for d in state.devices.values() if d.facility_id == lab_id]
        device_breakdown = [
            {
                "device_id": d.device_id,
                "device_type": d.device_type,
                "annual_capacity": d.annual_capacity(self.config.working_days_per_year),
                "shifts": d.max_shifts,
            }
            for d in lab_devices
        ]
        
        # Get demand breakdown by connected facility
        connected_facilities = []
        total_demand = 0
        
        for ref in state.referrals:
            if ref.lab_id == lab_id:
                facility = state.facilities.get(ref.facility_id)
                facility_demand = state.test_volumes.get(ref.facility_id, 0)
                total_demand += facility_demand
                
                connected_facilities.append({
                    "facility_id": ref.facility_id,
                    "facility_name": facility.name if facility else "Unknown",
                    "demand": facility_demand,
                    "percentage_of_total_demand": (facility_demand / total_demand * 100) if total_demand > 0 else 0,
                }
                )
        
        # Sort by demand (highest first)
        connected_facilities.sort(key=lambda x: x["demand"], reverse=True)
        
        # Calculate utilization
        utilization = total_demand / capacity if capacity > 0 else 0.0
        
        return {
            "lab_id": lab_id,
            "lab_name": lab.name,
            "utilization": utilization,
            "utilization_percent": utilization * 100,
            "capacity": capacity,
            "total_demand": total_demand,
            "demand_gap": total_demand - capacity,
            "device_count": len(lab_devices),
            "device_breakdown": device_breakdown,
            "connected_facility_count": len(connected_facilities),
            "connected_facilities": connected_facilities,
            "status": "overutilized" if utilization > self.threshold_high else
                     "underutilized" if utilization < self.threshold_low else
                     "balanced",
        }
    
    def explain_utilization(self, lab_id: str, state: NetworkState) -> str:
        """
        Generate human-readable explanation of lab utilization.
        
        Args:
            lab_id: Lab ID to explain
            state: Current network state
            
        Returns:
            Human-readable explanation string
        """
        breakdown = self.lab_utilization_breakdown(lab_id, state)
        
        if "error" in breakdown:
            return breakdown["error"]
        
        # Build explanation
        lab_name = breakdown["lab_name"]
        utilization_pct = breakdown["utilization_percent"]
        capacity = breakdown["capacity"]
        demand = breakdown["total_demand"]
        device_count = breakdown["device_count"]
        facility_count = breakdown["connected_facility_count"]
        status = breakdown["status"]
        
        # Status description
        if status == "overutilized":
            status_desc = f"**OVERUTILIZED** ({utilization_pct:.1f}% - above {self.threshold_high*100:.0f}% threshold)"
        elif status == "underutilized":
            status_desc = f"**UNDERUTILIZED** ({utilization_pct:.1f}% - below {self.threshold_low*100:.0f}% threshold)"
        else:
            status_desc = f"**BALANCED** ({utilization_pct:.1f}% - within thresholds)"
        
        explanation = f"""
Lab: {lab_name}
Status: {status_desc}

Utilization Calculation:
  • Total Demand: {demand:,} tests/year from {facility_count} connected facilities
  • Total Capacity: {capacity:,} tests/year from {device_count} device(s)
  • Utilization: {demand:,} ÷ {capacity:,} = {utilization_pct:.1f}%
"""
        
        # Add demand gap if overutilized
        if status == "overutilized":
            gap = breakdown["demand_gap"]
            explanation += f"\n  ⚠️  Demand Gap: {gap:,} tests/year (needs additional capacity)\n"
        
        # Add top contributing facilities
        top_facilities = breakdown["connected_facilities"][:5]
        if top_facilities:
            explanation += "\nTop Contributing Facilities:\n"
            for i, fac in enumerate(top_facilities, 1):
                explanation += f"  {i}. {fac['facility_name']}: {fac['demand']:,} tests/year ({fac['percentage_of_total_demand']:.1f}% of total)\n"
        
        # Add device breakdown
        if breakdown["device_breakdown"]:
            explanation += "\nDevice Capacity Breakdown:\n"
            for dev in breakdown["device_breakdown"]:
                explanation += f"  • {dev['device_type']}: {dev['annual_capacity']:,} tests/year ({dev['shifts']} shift(s))\n"
        
        return explanation.strip()
