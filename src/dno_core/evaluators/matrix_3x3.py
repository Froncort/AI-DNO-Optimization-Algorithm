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
3x3 Matrix evaluator for comprehensive optimization assessment.
"""

from typing import List, Optional, Union, Any
from decimal import Decimal
import logging

from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.goals import (
    OptimizationGoal,
    CapacityGoal,
    DevicePlacementGoal,
    CoverageGoal,
    UtilizationBalanceGoal,
    DemandProjectionGoal,
    BudgetConstrainedGoal,
    DynamicScenarioConfig,
)
from dno_core.models.actions import (
    RelocationAction, 
    FacilityConversionAction, 
    ExpansionAction, 
    RedesignAction
)
from dno_core.models.results import (
    Matrix3x3Result,
    FinancialAxisScore,
    ProgrammaticAxisScore,
    ImpactAxisScore,
)
from dno_core.calculators.cost import CostCalculator
from dno_core.calculators.capacity import CapacityCalculator
from dno_core.evaluators.utilization import UtilizationEvaluator
from dno_core.evaluators.accessibility import AccessibilityEvaluator
from dno_core.evaluators.rre import RREEvaluator

logger = logging.getLogger(__name__)

Action = Union[RelocationAction, FacilityConversionAction, ExpansionAction, RedesignAction]

# Partial placement is success when structural search stopped for optimality/feasibility
_PLACEMENT_PARTIAL_OK_STOP_REASONS = frozenset({
    "device_placement_consecutive_empty_expansions",
    "no_structural_candidates",
    "no_improvement_vs_min_threshold",
    "quota_satisfied",
})

_PLACEMENT_BLOCKED_REASON_MAP = {
    "device_placement_consecutive_empty_expansions": "optimality_reached_no_beneficial_site",
    "no_structural_candidates": "no_feasible_site",
    "no_improvement_vs_min_threshold": "optimality_reached_no_beneficial_site",
}


def _placement_spec_device_types(goal: DevicePlacementGoal) -> set:
    """Catalogue device type names requested by the placement goal."""
    types: set = set()
    items = [x for x in (goal.placement_items or []) if isinstance(x, dict)]
    if items:
        for it in items:
            dt = str(it.get("device_type") or "").strip().lower()
            if dt:
                types.add(dt)
    else:
        dt = str(goal.device_type or "").strip().lower()
        if dt:
            types.add(dt)
    return types


def _count_placement_expansion_actions(
    actions: List[Action], goal: DevicePlacementGoal
) -> int:
    """Count devices placed via expansion actions matching the goal spec."""
    spec_types = _placement_spec_device_types(goal)
    placed = 0
    for action in actions:
        if not isinstance(action, ExpansionAction):
            continue
        dt = str(action.device_type or "").strip().lower()
        if spec_types and dt not in spec_types:
            continue
        placed += max(1, int(action.quantity or 1))
    return placed


def _action_capex_total(actions: Optional[List[Action]]) -> float:
    """One-off CAPEX from committed actions (relocations + conversions + expansion startup)."""
    total = Decimal("0")
    for action in actions or []:
        if isinstance(action, RelocationAction):
            total += getattr(action, "total_relocation_cost", Decimal("0")) or Decimal("0")
        elif isinstance(action, FacilityConversionAction):
            total += getattr(action, "estimated_conversion_cost", Decimal("0")) or Decimal("0")
        elif isinstance(action, ExpansionAction):
            total += getattr(action, "total_startup_cost", Decimal("0")) or Decimal("0")
    return float(total)


class Matrix3x3Evaluator:
    """
    Evaluate optimization results on three axes:
    
    1. Financial: Cost efficiency
    2. Programmatic: Feasibility and policy compliance
    3. Impact: Effectiveness and goal achievement (includes RRE)
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize 3x3 matrix evaluator.
        
        Args:
            config: Engine configuration
        """
        self.config = config
        self.cost_calc = CostCalculator(config)
        self.capacity_calc = CapacityCalculator(config)
        self.util_eval = UtilizationEvaluator(config)
        self.access_eval = AccessibilityEvaluator(config)
        self.rre_eval = RREEvaluator(config)
    
    def evaluate(
        self,
        baseline_state: NetworkState,
        optimized_state: NetworkState,
        actions: List[Action],
        goal: Optional[OptimizationGoal] = None
    ) -> Matrix3x3Result:
        """
        Perform full 3x3 matrix evaluation.
        
        Args:
            baseline_state: State before optimization
            optimized_state: State after optimization
            actions: List of actions taken
            goal: Optional optimization goal
            
        Returns:
            Matrix3x3Result with all scores and optimization configuration
        """
        financial = self._calculate_financial_score(baseline_state, optimized_state, actions)
        programmatic = self._calculate_programmatic_score(actions)
        impact = self._calculate_impact_score(baseline_state, optimized_state, goal, actions)
        
        result = Matrix3x3Result(
            financial=financial,
            programmatic=programmatic,
            impact=impact,
        )
        
        # Add optimization configuration metadata
        if goal:
            result.optimization_mode = goal.optimization_mode if hasattr(goal, 'optimization_mode') else "utilization"
            result.objective_weights = goal.objective_weights if hasattr(goal, 'objective_weights') else None
        
        result.calculate_overall_score()
        result.recommendation = self._generate_recommendation(result)
        result.pros, result.cons = self._generate_pros_cons(result, actions)
        
        return result
    
    def _calculate_financial_score(
        self,
        baseline: NetworkState,
        optimized: NetworkState,
        actions: List[Action]
    ) -> FinancialAxisScore:
        """Calculate financial axis score."""
        baseline_breakdown = self.cost_calc.calculate_cost_breakdown(baseline)
        optimized_breakdown = self.cost_calc.calculate_cost_breakdown(optimized)
        
        baseline_total = baseline_breakdown["total_cost"]
        optimized_total = optimized_breakdown["total_cost"]
        
        sample_transport_baseline = baseline_breakdown["sample_transport_cost"]
        sample_transport_optimized = optimized_breakdown["sample_transport_cost"]
        transport_cost_delta = sample_transport_optimized - sample_transport_baseline
        transport_cost_delta_percent = (
            float(transport_cost_delta / sample_transport_baseline * 100)
            if sample_transport_baseline > 0
            else 0.0
        )
        
        # One-off CAPEX: relocations and facility conversions only.
        # ExpansionAction startup is intentionally excluded here: optimized_breakdown["total_cost"]
        # already includes annualised startup for all is_new devices (computed via
        # CostCalculator.calculate_cost_breakdown → device_annualized_startup_cost). Adding
        # action.total_startup_cost again would double-count every expansion's start-up.
        capex_cost = Decimal("0")
        # Annualized device investment cost (for score — compares apple-to-apple with annual savings)
        annualized_device_cost = Decimal("0")
        for action in actions:
            if isinstance(action, RelocationAction):
                capex_cost += action.total_relocation_cost
            elif isinstance(action, FacilityConversionAction):
                capex_cost += action.estimated_conversion_cost
            elif isinstance(action, ExpansionAction):
                # Not added to capex_cost — already in optimized_total via is_new device breakdown.
                annualized_device_cost += (
                    getattr(action, "annualized_startup_cost", Decimal("0"))
                    + getattr(action, "annual_operational_cost", Decimal("0"))
                )

        total_cost = optimized_total + capex_cost
        cost_delta = total_cost - baseline_total
        cost_delta_percent = float(cost_delta / baseline_total * 100) if baseline_total > 0 else 0

        # --- Smarter financial score ---
        # The raw cost_delta_percent is dominated by one-off device CAPEX which makes even
        # good runs look expensive.  Instead we score on the *recurring* annual economics:
        #
        #   annual_transport_savings  = baseline_transport - optimized_transport  (positive = saved)
        #   annual_device_investment  = annualized_startup + annual_operational   (positive = spent)
        #   net_annual_benefit        = transport_savings - device_investment
        #
        # Score anchors:
        #   • Net benefit ≥ 0  (transport savings cover annual device cost)  → 70–100
        #   • Net benefit = -baseline_transport  (all savings eaten + equal again) → 50 (neutral)
        #   • Full cost blowout                                                   → 0
        #
        # We also give a secondary penalty for the overall total_cost increase so that
        # cost_delta_percent still shows in the breakdown and affects the score slightly.
        transport_savings = float(sample_transport_baseline - sample_transport_optimized)
        annual_device_inv = float(annualized_device_cost)
        net_annual_benefit = transport_savings - annual_device_inv

        # Normalise net_annual_benefit against baseline transport to get a comparable ratio.
        # When no transport baseline exists fall back to total_cost_delta scoring.
        if float(sample_transport_baseline) > 0:
            # net_ratio > 0: net savings; net_ratio < 0: net cost
            net_ratio = net_annual_benefit / float(sample_transport_baseline)
            # Map: net_ratio=+1 (saved as much as whole baseline transport) → 90
            #       net_ratio=0 (break-even on annual basis)                → 70
            #       net_ratio=-1 (spent as much as baseline transport)      → 50
            #       net_ratio=-5                                            → ~0
            annual_score = max(0.0, min(100.0, 70.0 + net_ratio * 20.0))
            # Small secondary adjustment: raw total cost delta (capex included)
            raw_penalty = max(0.0, cost_delta_percent * 0.15)
            score = max(0, min(100, int(annual_score - raw_penalty)))
        else:
            score = max(0, min(100, 50 - cost_delta_percent * 1.0))
        
        total_tests = sum(optimized.test_volumes.values())
        cost_per_test = total_cost / total_tests if total_tests > 0 else Decimal("0")
        
        # Calculate baseline cost per test
        baseline_total_tests = sum(baseline.test_volumes.values())
        baseline_cost_per_test = baseline_total / baseline_total_tests if baseline_total_tests > 0 else Decimal("0")
        
        return FinancialAxisScore(
            total_cost=total_cost,
            testing_cost=optimized_breakdown["testing_cost"],
            transport_cost=sample_transport_optimized,
            operational_cost=optimized_breakdown["operational_cost"],
            startup_cost=optimized_breakdown["startup_cost_annualized"],
            sample_transport_cost=sample_transport_baseline,
            sample_transport_cost_optimized=sample_transport_optimized,
            transport_cost_delta=transport_cost_delta,
            transport_cost_delta_percent=transport_cost_delta_percent,
            capex_cost=capex_cost,
            baseline_total_cost=baseline_total,
            cost_delta=cost_delta,
            cost_delta_percent=cost_delta_percent,
            cost_per_test=cost_per_test,
            baseline_cost_per_test=baseline_cost_per_test,
            score=score,
        )
    
    def _calculate_programmatic_score(
        self,
        actions: List[Action]
    ) -> ProgrammaticAxisScore:
        """Calculate programmatic axis score."""
        relocations = [a for a in actions if isinstance(a, RelocationAction)]
        conversions = [a for a in actions if isinstance(a, FacilityConversionAction)]
        expansions = [a for a in actions if isinstance(a, ExpansionAction)]
        redesigns = [a for a in actions if isinstance(a, RedesignAction)]
        
        # Count affected labs
        affected_labs = set()
        for r in relocations:
            affected_labs.add(r.source_lab_id)
            affected_labs.add(r.target_lab_id)
        for c in conversions:
            affected_labs.add(c.facility_id)
        for e in expansions:
            affected_labs.add(e.lab_id)
        
        # Check policy compliance
        violations = []
        cross_boundary = sum(1 for r in redesigns if r.crosses_admin_boundary)
        
        if not self.config.allow_cross_boundary and cross_boundary > 0:
            violations.append(f"{cross_boundary} cross-boundary assignments")
        
        policy_compliant = len(violations) == 0
        
        # Score based on feasibility (fewer actions, better compliance = higher)
        # More lenient: reduce penalty per action from 5 to 2.5, increase base score
        action_count = len(actions)
        action_penalty = min(40, action_count * 2.5)  # More actions = more complexity (reduced penalty)
        compliance_bonus = 30 if policy_compliant else 0
        
        score = max(0, min(100, 80 - action_penalty + compliance_bonus))  # Higher base score (80 vs 70)
        
        return ProgrammaticAxisScore(
            relocations_count=len(relocations),
            conversions_count=len(conversions),
            expansions_count=len(expansions),
            redesigns_count=len(redesigns),
            conversion_eligible_count=len(conversions),
            labs_affected=len(affected_labs),
            policy_compliant=policy_compliant,
            policy_violations=violations,
            cross_boundary_actions=cross_boundary,
            score=score,
        )
    
    def _calculate_impact_score(
        self,
        baseline: NetworkState,
        optimized: NetworkState,
        goal: Optional[OptimizationGoal] = None,
        actions: Optional[List[Action]] = None,
    ) -> ImpactAxisScore:
        """Calculate impact axis score including RRE."""
        # Capacity comparison
        baseline_capacity = self.capacity_calc.network_capacity(baseline)
        final_capacity = self.capacity_calc.network_capacity(optimized)
        capacity_change = final_capacity - baseline_capacity
        capacity_change_pct = (capacity_change / baseline_capacity * 100) if baseline_capacity > 0 else 0
        
        # Utilization comparison
        baseline_util = self.util_eval.evaluate(baseline)
        final_util = self.util_eval.evaluate(optimized)
        
        baseline_avg_util = baseline_util.overall_utilization
        final_avg_util = final_util.overall_utilization
        util_improvement = baseline_avg_util - final_avg_util  # Lower is better if was overutilized
        
        # Overutilization reduction
        baseline_overutil = len(baseline_util.overutilized_labs)
        final_overutil = len(final_util.overutilized_labs)
        overutil_reduction = baseline_overutil - final_overutil
        
        # Accessibility improvement using ASD-based score
        # Reset baseline to get independent measurements
        self.access_eval.baseline_score = None
        baseline_acc_report = self.access_eval.evaluate(baseline)
        baseline_acc = baseline_acc_report.get("accessibility_score", 0.0)
        baseline_asd = float(baseline_acc_report.get("avg_distance_km", 0.0) or 0.0)
        pct10_b = float(baseline_acc_report.get("pct_within_10km", 0.0) or 0.0)
        pct25_b = float(baseline_acc_report.get("pct_within_25km", 0.0) or 0.0)
        pct50_b = float(baseline_acc_report.get("pct_within_50km", 0.0) or 0.0)
        
        # Reset again for final evaluation
        self.access_eval.baseline_score = None
        final_acc_report = self.access_eval.evaluate(optimized)
        final_acc = final_acc_report.get("accessibility_score", 0.0)
        final_asd = float(final_acc_report.get("avg_distance_km", 0.0) or 0.0)
        pct10_o = float(final_acc_report.get("pct_within_10km", 0.0) or 0.0)
        pct25_o = float(final_acc_report.get("pct_within_25km", 0.0) or 0.0)
        pct50_o = float(final_acc_report.get("pct_within_50km", 0.0) or 0.0)
        asd_change_km = baseline_asd - final_asd
        
        # Calculate improvement (positive = better accessibility = lower ASD)
        if baseline_acc > 0:
            access_improvement = ((final_acc - baseline_acc) / baseline_acc) * 100
        else:
            access_improvement = 0.0
        
        logger.info(f"Accessibility: baseline={baseline_acc:.1f}%, final={final_acc:.1f}%, improvement={access_improvement:+.2f}%")
        logger.info(f"  ASD: {baseline_asd:.1f}km → {final_asd:.1f}km")
        
        logger.info(f"Accessibility: baseline={baseline_acc:.2f}%, final={final_acc:.2f}%, improvement={access_improvement:.2f}%")
        
        # New facilities with access
        baseline_access_count = baseline_acc_report.get("facilities_with_access", 0)
        final_access_count = final_acc_report.get("facilities_with_access", 0)
        new_facilities_with_access = max(0, final_access_count - baseline_access_count)
        
        # RRE (Referral Redesign Efficiency)
        rre_metrics = self.rre_eval.calculate_rre(baseline, optimized)
        rre_score = rre_metrics["rre_score"]
        distance_saved = rre_metrics["distance_saved_km"]
        
        # Goal achievement
        goal_achievement = 0.0
        goal_met = False
        placement_requested = 0
        placement_placed = 0
        remaining_quota = 0
        placement_blocked: Optional[str] = None

        is_capacity_goal = goal and isinstance(goal, CapacityGoal)
        is_placement_goal = goal and isinstance(goal, DevicePlacementGoal)
        is_coverage_goal = goal and isinstance(goal, CoverageGoal)
        is_util_goal = goal and isinstance(goal, UtilizationBalanceGoal)
        is_demand_proj_goal = goal and isinstance(goal, DemandProjectionGoal)
        is_budget_goal = goal and isinstance(goal, BudgetConstrainedGoal)
        is_manual_goal = goal and isinstance(goal, DynamicScenarioConfig)

        if is_capacity_goal:
            target = goal.target_annual_tests
            achieved = final_capacity
            goal_achievement = min(100, achieved / target * 100) if target > 0 else 0
            goal_met = final_capacity >= target
        elif is_coverage_goal:
            # Lazy import breaks results_exporter ↔ matrix_3x3 cycle via evaluators.__init__
            from dno_core.domain.coverage_split import compute_coverage_split

            cov = compute_coverage_split(optimized, threshold_km=goal.max_distance_km)
            total = int(cov.get("total_in_scope_facilities", 0) or 0)
            within = int(cov.get("within_facilities", 0) or 0) + int(
                cov.get("onsite_facilities", 0) or 0
            )
            pct = (within / total * 100.0) if total > 0 else 0.0
            target = float(goal.target_coverage_percent or 100.0)
            goal_achievement = min(100.0, pct / target * 100.0) if target > 0 else 0.0
            goal_met = pct >= target
            if total == 0:
                logger.info("Coverage goal_met: no in-scope facilities with volume > 0")
        elif is_util_goal:
            th_high = float(goal.max_utilization_target)
            th_low = float(goal.min_utilization_target)
            util_goal = self.util_eval.evaluate(
                optimized, threshold_high=th_high, threshold_low=th_low
            )
            goal_met = (
                len(util_goal.overutilized_labs) == 0
                and th_low <= final_avg_util <= th_high
            )
            if th_high > th_low:
                band_mid = (th_high + th_low) / 2.0
                dist = abs(final_avg_util - band_mid) / max(band_mid, 1e-6)
                goal_achievement = max(0.0, min(100.0, (1.0 - dist) * 100.0))
            else:
                goal_achievement = 100.0 if goal_met else 0.0
        elif is_demand_proj_goal:
            projected_demand = sum(int(v or 0) for v in optimized.test_volumes.values())
            goal_achievement = (
                min(100.0, final_capacity / projected_demand * 100.0)
                if projected_demand > 0
                else 0.0
            )
            goal_met = final_capacity >= projected_demand if projected_demand > 0 else False
        elif is_budget_goal:
            capex = _action_capex_total(actions)
            max_b = float(goal.max_budget or 0)
            under_budget = capex <= max_b if max_b > 0 else True
            improved = (
                rre_score > 0
                or capacity_change > 0
                or overutil_reduction > 0
                or access_improvement > 0
            )
            goal_met = under_budget and improved
            goal_achievement = (
                min(100.0, max(0.0, (1.0 - capex / max_b) * 100.0))
                if max_b > 0 and under_budget
                else (50.0 if improved else 0.0)
            )
        elif is_manual_goal:
            improved = (
                capacity_change > 0
                or overutil_reduction > 0
                or access_improvement > 0
                or rre_score > 0
            )
            goal_met = bool(actions) and improved
            goal_achievement = min(
                100.0,
                (30.0 if capacity_change > 0 else 0.0)
                + (25.0 if overutil_reduction > 0 else 0.0)
                + (25.0 if access_improvement > 0 else 0.0)
                + (20.0 if rre_score > 0 else 0.0),
            )
        elif is_placement_goal:
            placement_requested = int(goal.total_placement_quantity or 0)
            placement_placed = _count_placement_expansion_actions(actions or [], goal)
            remaining_quota = max(0, placement_requested - placement_placed)
            goal_achievement = (
                min(100.0, placement_placed / placement_requested * 100.0)
                if placement_requested > 0
                else 0.0
            )
            stop_reason = str(getattr(goal, "_placement_stop_reason", "") or "")
            blocked = _PLACEMENT_BLOCKED_REASON_MAP.get(stop_reason)
            if placement_placed >= placement_requested:
                goal_met = True
            elif placement_placed > 0 and stop_reason in _PLACEMENT_PARTIAL_OK_STOP_REASONS:
                goal_met = False
                placement_blocked = blocked
            else:
                goal_met = False
                placement_blocked = blocked or (
                    "invalid_spec" if not goal.has_actionable_spec() else stop_reason or "placement_incomplete"
                )
        
        # Calculate overall impact score with RRE
        score_components = []
        
        # For capacity goals, adjust weights to focus on capacity achievement
        if is_capacity_goal:
            # Capacity score (25% weight - increased for capacity goals)
            capacity_score = min(100, max(0, 50 + capacity_change_pct * 2))
            score_components.append(capacity_score * 0.25)
            
            # Utilization balance score (20% weight - reduced)
            # More lenient: reduce penalty per overutilized lab from 20 to 10 points
            util_score = max(0, 100 - (final_overutil * 10))
            score_components.append(util_score * 0.20)
            
            # Goal achievement score (40% weight - heavily weighted for capacity goals)
            score_components.append(goal_achievement * 0.40)
            
            # Accessibility score (10% weight - reduced, don't penalize for capacity goals)
            # More lenient: don't penalize negative changes, give neutral score (50) for no change
            access_score = min(100, max(50, 50 + max(0, access_improvement) * 3))  # Don't penalize negative, more reward for positive
            score_components.append(access_score * 0.10)
            
            # RRE score (5% weight - minimal weight for capacity goals since redesign is disabled)
            # Only count positive RRE, don't penalize negative
            rre_points = min(100, max(0, rre_score * 333)) if rre_score >= 0 else 50  # Neutral if negative
            score_components.append(rre_points * 0.05)
        else:
            # Standard scoring for non-capacity goals
            # Capacity score (15% weight - reduced to make room for RRE)
            capacity_score = min(100, max(0, 50 + capacity_change_pct * 2))
            score_components.append(capacity_score * 0.15)
            
            # Utilization balance score (25% weight)
            # More lenient: reduce penalty per overutilized lab from 20 to 10 points
            util_score = max(0, 100 - (final_overutil * 10))
            score_components.append(util_score * 0.25)
            
            # Goal achievement score (25% weight)
            score_components.append(goal_achievement * 0.25)
            
            # Accessibility score (15% weight)
            # More lenient: don't penalize negative changes harshly, give neutral score (50) for no change
            access_score = min(100, max(40, 50 + access_improvement * 2.5))  # Floor at 40, more reward for positive
            score_components.append(access_score * 0.15)
            
            # RRE score (20% weight - NEW!)
            # RRE of 0.15 (15% distance reduction) = 75 points
            # RRE of 0.30 (30% distance reduction) = 100 points
            rre_points = min(100, max(0, rre_score * 333))  # 0.3 RRE = 100 points
            score_components.append(rre_points * 0.20)
        
        score = sum(score_components)
        
        return ImpactAxisScore(
            baseline_capacity=baseline_capacity,
            final_capacity=final_capacity,
            capacity_change=capacity_change,
            capacity_change_percent=capacity_change_pct,
            baseline_avg_utilization=baseline_avg_util,
            final_avg_utilization=final_avg_util,
            utilization_improvement=util_improvement,
            baseline_overutilized_labs=baseline_overutil,
            final_overutilized_labs=final_overutil,
            overutilization_reduction=overutil_reduction,
            baseline_accessibility=baseline_acc,
            final_accessibility=final_acc,
            accessibility_improvement=access_improvement,
            new_facilities_with_access=new_facilities_with_access,
            goal_achievement_percent=goal_achievement,
            goal_met=goal_met,
            placement_requested_count=placement_requested,
            placement_placed_count=placement_placed,
            remaining_device_quota=remaining_quota,
            placement_blocked_reason=placement_blocked,
            score=score,
            rre_score=rre_score,
            distance_saved_km=distance_saved,
            asd_baseline_km=baseline_asd,
            asd_optimized_km=final_asd,
            asd_change_km=asd_change_km,
            pct_within_10km_baseline=pct10_b,
            pct_within_10km_optimized=pct10_o,
            pct_within_25km_baseline=pct25_b,
            pct_within_25km_optimized=pct25_o,
            pct_within_50km_baseline=pct50_b,
            pct_within_50km_optimized=pct50_o,
        )
    
    def _generate_recommendation(self, result: Matrix3x3Result) -> str:
        """Generate recommendation based on scores."""
        if result.overall_score >= 80:
            return "Strongly recommended - excellent balance of cost, feasibility, and impact"
        elif result.overall_score >= 60:
            return "Recommended with minor adjustments"
        elif result.overall_score >= 40:
            return "Proceed with caution - review trade-offs carefully"
        else:
            return "Not recommended - consider alternative approaches"
    
    def _generate_pros_cons(
        self, 
        result: Matrix3x3Result,
        actions: List[Action]
    ) -> tuple[List[str], List[str]]:
        """Generate pros and cons for XAI."""
        pros = []
        cons = []
        
        # Financial
        if result.financial.cost_delta_percent < 0:
            pros.append(f"Reduces costs by {abs(result.financial.cost_delta_percent):.1f}%")
        elif result.financial.cost_delta_percent > 10:
            cons.append(f"Increases costs by {result.financial.cost_delta_percent:.1f}%")
        
        # Impact
        if result.impact.overutilization_reduction > 0:
            pros.append(f"Reduces overutilized labs by {result.impact.overutilization_reduction}")
        
        if result.impact.capacity_change > 0:
            pros.append(f"Adds {result.impact.capacity_change:,} tests/year capacity")
        
        if result.impact.goal_met:
            pros.append("Achieves optimization goal")
        elif result.impact.goal_achievement_percent > 0:
            cons.append(f"Only {result.impact.goal_achievement_percent:.1f}% of goal achieved")
        if not result.impact.goal_met and result.impact.goal_achievement_percent < 100:
            cons.append("Optimization goal not fully met")
        
        # Programmatic
        if result.programmatic.policy_compliant:
            pros.append("Fully compliant with policies")
        else:
            cons.extend(result.programmatic.policy_violations)
        
        if result.programmatic.labs_affected > 5:
            cons.append(f"Affects {result.programmatic.labs_affected} labs - high complexity")
        
        return pros, cons
    
    def explain_matrix(
        self,
        result: Matrix3x3Result,
        baseline_state: NetworkState,
        optimized_state: NetworkState
    ) -> str:
        """
        Generate human-readable explanation of 3x3 matrix evaluation.
        
        Args:
            result: Matrix3x3Result from evaluate()
            baseline_state: Baseline network state
            optimized_state: Optimized network state
            
        Returns:
            Human-readable explanation string
        """
        explanation = f"""
3x3 Optimization Matrix Analysis

Overall Score: {result.overall_score:.1f}/100
Recommendation: {result.recommendation}

═══════════════════════════════════════════════════════════
1. FINANCIAL AXIS (Cost Efficiency)
═══════════════════════════════════════════════════════════

Score: {result.financial.score:.1f}/100

Cost Breakdown:
  • Total Cost: ₹{float(result.financial.total_cost):,.0f}
    - Testing Cost: ₹{float(result.financial.testing_cost):,.0f}
    - Sample transport (annual): ₹{float(result.financial.transport_cost):,.0f}
    - CAPEX (this run): ₹{float(result.financial.capex_cost):,.0f}
    - Operational Cost: ₹{float(result.financial.operational_cost):,.0f}
    - Startup Cost (one-time): ₹{float(result.financial.startup_cost):,.0f}

Comparison to Baseline:
  • Baseline Total Cost: ₹{float(result.financial.baseline_total_cost):,.0f}
  • Cost Change: ₹{float(result.financial.cost_delta):,.0f} ({result.financial.cost_delta_percent:+.1f}%)
"""
        
        # Financial interpretation
        if result.financial.cost_delta_percent < 0:
            explanation += f"\n✅ Cost reduced by {abs(result.financial.cost_delta_percent):.1f}% - more financially efficient\n"
        elif result.financial.cost_delta_percent > 10:
            explanation += f"\n⚠️  Cost increased by {result.financial.cost_delta_percent:.1f}% - less financially efficient\n"
        else:
            explanation += f"\n➡️  Cost roughly maintained ({result.financial.cost_delta_percent:+.1f}% change)\n"
        
        explanation += f"""

═══════════════════════════════════════════════════════════
2. PROGRAMMATIC AXIS (Feasibility & Compliance)
═══════════════════════════════════════════════════════════

Score: {result.programmatic.score:.1f}/100

Actions Required:
  • Relocations: {result.programmatic.relocations_count}
  • Conversions: {result.programmatic.conversions_count}
  • Expansions: {result.programmatic.expansions_count}
  • Redesigns: {result.programmatic.redesigns_count}
  • Total Labs Affected: {result.programmatic.labs_affected}

Policy Compliance:
  • Status: {"✅ COMPLIANT" if result.programmatic.policy_compliant else "❌ VIOLATIONS DETECTED"}
  • Cross-Boundary Actions: {result.programmatic.cross_boundary_actions}
"""
        
        if result.programmatic.policy_violations:
            explanation += "\nPolicy Violations:\n"
            for violation in result.programmatic.policy_violations:
                explanation += f"  ⚠️  {violation}\n"
        else:
            explanation += "  ✅ All policy constraints satisfied\n"
        
        explanation += f"""

═══════════════════════════════════════════════════════════
3. IMPACT AXIS (Effectiveness & Goal Achievement)
═══════════════════════════════════════════════════════════

Score: {result.impact.score:.1f}/100

Capacity Impact:
  • Baseline Capacity: {result.impact.baseline_capacity:,} tests/year
  • Final Capacity: {result.impact.final_capacity:,} tests/year
  • Change: {result.impact.capacity_change:+,} tests/year ({result.impact.capacity_change_percent:+.1f}%)

Utilization Impact:
  • Baseline Avg Utilization: {result.impact.baseline_avg_utilization:.1%}
  • Final Avg Utilization: {result.impact.final_avg_utilization:.1%}
  • Overutilized Labs: {result.impact.baseline_overutilized_labs} → {result.impact.final_overutilized_labs}
  • Reduction: {result.impact.overutilization_reduction} labs

Accessibility Impact:
  • Baseline Accessibility Score: {result.impact.baseline_accessibility:.4f}
  • Final Accessibility Score: {result.impact.final_accessibility:.4f}
  • Improvement: {result.impact.accessibility_improvement:+.1f}%

Referral Efficiency (RRE):
  • RRE Score: {result.impact.rre_score:.4f} ({result.impact.rre_score*100:.1f}%)
  • Distance Saved: {result.impact.distance_saved_km:.1f} km

ASD and coverage:
  • ASD: {result.impact.asd_baseline_km:.2f} km → {result.impact.asd_optimized_km:.2f} km (Δ {result.impact.asd_change_km:+.2f} km)
  • % within 10 km: {result.impact.pct_within_10km_baseline:.1f}% → {result.impact.pct_within_10km_optimized:.1f}%
  • % within 25 km: {result.impact.pct_within_25km_baseline:.1f}% → {result.impact.pct_within_25km_optimized:.1f}%
  • % within 50 km: {result.impact.pct_within_50km_baseline:.1f}% → {result.impact.pct_within_50km_optimized:.1f}%
"""
        
        if hasattr(result.impact, 'goal_achievement_percent') and result.impact.goal_achievement_percent > 0:
            goal_status = "✅ ACHIEVED" if result.impact.goal_met else f"⚠️  PARTIAL ({result.impact.goal_achievement_percent:.1f}%)"
            explanation += f"\nGoal Achievement: {goal_status}\n"
        
        # Comparative explanation
        explanation += "\n" + "="*63 + "\n"
        explanation += "WHY THE OPTIMIZED NETWORK IS"
        
        if result.overall_score >= 70:
            explanation += " BETTER:\n" + "="*63 + "\n"
            for pro in result.pros:
                explanation += f"  ✅ {pro}\n"
        else:
            explanation += " DIFFERENT (Trade-offs):\n" + "="*63 + "\n"
            
        explanation += "\nPros:\n"
        for pro in result.pros:
            explanation += f"  ✅ {pro}\n"
        
        if result.cons:
            explanation += "\nCons:\n"
            for con in result.cons:
                explanation += f"  ⚠️  {con}\n"
        
        return explanation.strip()
