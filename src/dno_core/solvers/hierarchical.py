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
Hierarchical solver with three-step optimization and convergence loop.
"""

from typing import List, Optional, Callable, Any, Union, Set, Dict, Tuple
from datetime import datetime
import logging
import time
import copy
from decimal import Decimal

from dno_core.models.config import EngineConfig, cap_config_for_large_network
from dno_core.models.network_state import NetworkState
from dno_core.models.goals import OptimizationGoal, DynamicScenarioConfig, DevicePlacementGoal
from dno_core.models.actions import (
    RelocationAction,
    FacilityConversionAction,
    ExpansionAction,
    RedesignAction,
    TestIntegrationAction,
    AddShiftAction,
)
from dno_core.models.results import OptimizationResult, UtilizationReport
from dno_core.solvers.milp_decomposition import (
    MILPDecompositionSolver,
    get_effective_milp_objective_weights,
    is_distance_first_open_referral,
    resolve_milp_boundary_level,
)
from dno_core.evaluators.utilization import UtilizationEvaluator
from dno_core.evaluators.matrix_3x3 import Matrix3x3Evaluator
from dno_core.evaluators.rre import RREEvaluator
from dno_core.calculators.capacity import CapacityCalculator
from dno_core.logging.progress import optimization_logger
from dno_core.domain.transport_referral_utils import (
    drop_outbound_referrals_for_lab_origins,
)

logger = logging.getLogger(__name__)


Action = Union[
    RelocationAction,
    FacilityConversionAction,
    ExpansionAction,
    RedesignAction,
    TestIntegrationAction,
    AddShiftAction,
]


def _effective_admin_area(entity, level: str) -> str:
    """admin_area_1 falls back to legacy `state`; admin_area_2 to `district`. "" if unknown."""
    if level == "admin_area_2":
        v = (getattr(entity, "admin_area_2", None) or "").strip()
        return v or (getattr(entity, "district", None) or "").strip()
    v = (getattr(entity, "admin_area_1", None) or "").strip()
    return v or (getattr(entity, "state", None) or "").strip()


def _is_boundary_violation(facility, lab, level: str) -> bool:
    """True only when BOTH areas are known and differ. Unknown area = exempt, never force-moved."""
    fa = _effective_admin_area(facility, level)
    la = _effective_admin_area(lab, level)
    return bool(fa) and bool(la) and fa != la


def _admin_boundaries_enforced(config) -> bool:
    return (
        getattr(config, "admin_boundary_enabled", False)
        and not getattr(config, "allow_cross_boundary", False)
    )


def _boundary_unenforceable_message(area: str, samples: float, referrals: int) -> str:
    """Concise user-facing message when no in-area testing site exists for boundary enforcement."""
    if samples > 0 and referrals > 0:
        stats = f"{samples:.0f} samples across {referrals} referrals"
    elif samples > 0:
        stats = f"{samples:.0f} samples"
    elif referrals > 0:
        stats = f"{referrals} referral{'s' if referrals != 1 else ''}"
    else:
        stats = "referrals"
    return (
        f"No in-area testing site; {stats} remain cross-boundary "
        f"until a site is added or converted here."
    )


class HierarchicalSolver:
    """
    OSS scope: Phase-1 MILP referral redesign + 3×3 evaluation.
    """
    
    def __init__(self, config: EngineConfig):
        self.config = config
        self.milp_solver = MILPDecompositionSolver(config)
        self.util_evaluator = UtilizationEvaluator(config)
        self.capacity_calc = CapacityCalculator(config)
        self.matrix_evaluator = Matrix3x3Evaluator(config)
        self.rre_evaluator = RREEvaluator(config)
    
    async def optimize(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> OptimizationResult:
        """
        Execute Phase-1 redesign and 3×3 evaluation (OSS open_referral scope).
        """
        return await self._run_optimization(state, goal, progress_callback)
    
    async def _run_admin_area_wise_optimization(
        self,
        state: NetworkState,
        goal: DynamicScenarioConfig,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> OptimizationResult:
        """
        Run optimization separately for each partition (admin_area_1 or admin_area_2).

        Prevents cross-boundary actions within the chosen boundary; MILP subgraphs use the same level.
        """
        import copy
        from dno_core.models.results import OptimizationResult
        
        boundary_level = getattr(goal, "optimize_admin_area_level", None) or "admin_area_1"
        if boundary_level not in ("admin_area_1", "admin_area_2"):
            boundary_level = "admin_area_1"
        level_label = "Admin Area 1" if boundary_level == "admin_area_1" else "Admin Area 2"

        logger.info("=" * 80)
        logger.info(f"{level_label.upper()} PARTITION OPTIMIZATION")
        logger.info("=" * 80)
        
        # Unique non-empty effective boundary values on facilities and labs
        # (legacy imports may only populate state/district — see _effective_admin_area).
        admin_areas: set = set()
        for facility in state.facilities.values():
            val = _effective_admin_area(facility, boundary_level)
            if val:
                admin_areas.add(val)
        for lab in state.labs.values():
            val = _effective_admin_area(lab, boundary_level)
            if val:
                admin_areas.add(val)

        empty_area_facilities = sum(
            1
            for f in state.facilities.values()
            if not _effective_admin_area(f, boundary_level)
        )
        if empty_area_facilities:
            logger.info(
                "%s facility(ies) have empty effective %s — excluded from partitions, "
                "exempt from boundary enforcement",
                empty_area_facilities,
                boundary_level,
            )
        
        admin_areas = sorted(list(admin_areas))
        logger.info(f"Found {len(admin_areas)} {level_label} partition(s): {admin_areas}")
        
        raw_ow_aa, eff_ow_aa = get_effective_milp_objective_weights(goal)
        logger.info(
            "Admin-area-wise optimization — objective weights (effective): "
            f"utilization={eff_ow_aa.get('utilization', 0):.1%}, "
            f"accessibility(distance)={eff_ow_aa.get('accessibility', 0):.1%}, "
            f"cost={eff_ow_aa.get('cost', 0):.1%}"
            + (f" | raw: {raw_ow_aa}" if raw_ow_aa else " | raw: (none → defaults)")
        )
        optimization_logger.log(
            state.project_id,
            "ADMIN_AREA_OPTIMIZATION",
            "started",
            {
                "objective_weights_raw": raw_ow_aa,
                "objective_weights_effective": eff_ow_aa,
                "admin_areas": admin_areas,
                "admin_area_count": len(admin_areas),
                "boundary_level": boundary_level,
            },
        )
        
        # Track combined results
        all_actions = []
        admin_area_results = {}
        combined_iteration_history: List[Dict[str, Any]] = []
        total_exec = 0.0
        sum_redesign_iters = 0
        sum_structural_rounds = 0
        
        # Run optimization for each partition
        for i, admin_area in enumerate(admin_areas, 1):
            logger.info("=" * 80)
            logger.info(f"{level_label.upper()} {i}/{len(admin_areas)}: {admin_area}")
            logger.info("=" * 80)
            
            area_state = self._filter_state_by_admin_boundary(state, admin_area, boundary_level)
            
            if len(area_state.labs) == 0:
                # Feasibility condition, not an error: report and skip solver for this partition.
                area_facility_ids = set(area_state.facilities.keys())
                unenforceable_samples = sum(
                    float(state.test_volumes.get(fid, 0))
                    for fid in area_facility_ids
                )
                unenforceable_referrals = sum(
                    1 for ref in state.referrals if ref.facility_id in area_facility_ids
                )
                admin_area_results[admin_area] = {
                    "admin_area": admin_area,
                    "facilities": len(area_state.facilities),
                    "labs": 0,
                    "actions": 0,
                    "boundary_unenforceable": True,
                    "boundary_unenforceable_reason": (
                        "No testing site/lab exists in this admin area, so boundary enforcement "
                        "cannot route these samples inside the boundary."
                    ),
                    "boundary_unenforceable_referrals": unenforceable_referrals,
                    "boundary_unenforceable_samples": unenforceable_samples,
                    "boundary_message": _boundary_unenforceable_message(
                        admin_area, unenforceable_samples, unenforceable_referrals
                    ),
                }
                logger.warning(
                    "No labs in %s — boundary enforcement unenforceable (%s facilities, %.0f samples)",
                    admin_area,
                    len(area_state.facilities),
                    unenforceable_samples,
                )
                continue
            
            logger.info(f"Admin area {admin_area}: {len(area_state.facilities)} facilities, {len(area_state.labs)} labs, {len(area_state.devices)} devices")
            
            # Create admin area specific goal (no cross-boundary actions)
            area_goal = copy.deepcopy(goal)
            area_goal.optimize_by_admin_area = False  # Prevent recursion
            
            redesign_impossible = (
                "redesign" in area_goal.optimization_modes
                and len(area_state.labs) < 2
            )
            if redesign_impossible:
                logger.warning(
                    f"Admin area '{admin_area}': only {len(area_state.labs)} lab(s). "
                    "Redesign cannot switch referrals between labs here — use relocation "
                    "or expansion (or add labs) to change routing."
                )
            
            # Run optimization for this admin area (MILP merge uses full state referrals)
            area_result = await self._run_optimization(
                area_state,
                area_goal,
                progress_callback,
                milp_assignment_reference_state=state,
                milp_boundary_level_override=boundary_level,
            )
            
            total_exec += float(area_result.execution_time_seconds or 0)
            sum_redesign_iters += int(getattr(area_result, "redesign_iterations", 0) or 0)
            sum_structural_rounds += int(getattr(area_result, "structural_search_rounds", 0) or 0)
            for h in area_result.iteration_history or []:
                combined_iteration_history.append({**h, "admin_area": admin_area})

            # Store results
            admin_area_results[admin_area] = {
                "admin_area": admin_area,
                "facilities": len(area_state.facilities),
                "labs": len(area_state.labs),
                "actions": len(area_result.actions),
                "iterations": int(getattr(area_result, "iterations", 0) or 0),
                "redesign_iterations": int(getattr(area_result, "redesign_iterations", 0) or 0),
                "structural_search_rounds": int(getattr(area_result, "structural_search_rounds", 0) or 0),
                "baseline_metrics": area_result.baseline_metrics,
                "final_metrics": area_result.final_metrics,
                "rre_score": getattr(area_result, 'rre_score', 0),
            }
            if redesign_impossible:
                admin_area_results[admin_area]["redesign_structurally_impossible"] = True
                admin_area_results[admin_area]["redesign_skipped_reason"] = (
                    "Only one lab in this admin area — MILP redesign cannot reassign between labs."
                )
            
            # Collect actions
            all_actions.extend(area_result.actions)
            
            logger.info(f"✓ {admin_area}: {len(area_result.actions)} actions generated")
        
        logger.info("=" * 80)
        logger.info(f"ADMIN AREA-WISE OPTIMIZATION COMPLETE: {len(all_actions)} total actions across {len(admin_areas)} areas")
        logger.info("=" * 80)
        
        # Apply all actions to full state for final evaluation
        final_state = state.copy()
        for action in all_actions:
            final_state = self._apply_action_to_state(final_state, action)

        # Post-pass safety net: certifies boundary compliance and repairs any residue
        # (skipped partitions, infeasible MILP, redesign-disabled runs, etc.).
        repair_actions, repair_stats = self._repair_cross_boundary_referrals(
            final_state, boundary_level
        )
        all_actions.extend(repair_actions)

        # Repair pass is authoritative for unenforceable counts — update existing rows only.
        for area, count in (repair_stats.get("unenforceable") or {}).items():
            if count <= 0:
                continue
            samples = (repair_stats.get("unenforceable_samples") or {}).get(area, 0)
            boundary_fields = {
                "boundary_unenforceable": True,
                "boundary_unenforceable_reason": (
                    "No testing site/lab exists in this admin area, so boundary enforcement "
                    "cannot route these samples inside the boundary."
                ),
                "boundary_unenforceable_referrals": count,
                "boundary_unenforceable_samples": samples,
                "boundary_message": _boundary_unenforceable_message(area, samples, count),
            }
            if area in admin_area_results:
                admin_area_results[area].update(boundary_fields)
            else:
                admin_area_results[area] = {"admin_area": area, **boundary_fields}

        for area, repairs in (repair_stats.get("repairs_by_area") or {}).items():
            if area in admin_area_results:
                admin_area_results[area]["boundary_repairs"] = repairs
            else:
                admin_area_results[area] = {
                    "admin_area": area,
                    "boundary_repairs": repairs,
                }

        optimization_logger.log(
            state.project_id,
            "BOUNDARY_ENFORCEMENT",
            "completed",
            repair_stats,
        )
        logger.info(
            "Boundary enforcement post-pass: repaired=%s unenforceable=%s unknown_area_referrals=%s",
            repair_stats.get("repaired", 0),
            repair_stats.get("unenforceable", {}),
            repair_stats.get("unknown_area_referrals", 0),
        )
        
        # Evaluate final state
        final_util = self.util_evaluator.evaluate(final_state)
        final_metrics = self._extract_metrics(final_state, final_util)
        
        # Merge partition-wise RRE metrics (keys must match partition dict)
        if boundary_level == "admin_area_1":
            admin_area_rre = self.rre_evaluator.calculate_rre_by_admin_area(state, final_state)
        else:
            admin_area_rre = self.rre_evaluator.calculate_rre_by_admin_area_2(state, final_state)
        for admin_area, rre_data in admin_area_rre.items():
            if admin_area in admin_area_results:
                admin_area_results[admin_area].update(rre_data)
        
        # Log summary per admin area
        logger.info("=" * 80)
        logger.info("ADMIN AREA SUMMARY:")
        for admin_area in sorted(admin_area_results.keys()):
            area_data = admin_area_results[admin_area]
            logger.info(f"  {admin_area}:")
            logger.info(f"    Actions: {area_data.get('actions', 0)}")
            logger.info(f"    ASD: {area_data.get('asd_baseline', 0):.1f}km → {area_data.get('asd_optimized', 0):.1f}km ({area_data.get('asd_improvement_percent', 0):+.1f}%)")
            logger.info(f"    RRE Score: {area_data.get('rre_score', 0):.2%}")
        logger.info("=" * 80)
        
        # Calculate 3x3 matrix for overall results
        matrix_3x3 = self.matrix_evaluator.evaluate(
            state,
            final_state,
            all_actions,
            goal
        )
        
        total_iters = sum_redesign_iters + sum_structural_rounds
        logger.info(
            "Admin-area-wise totals: redesign_iterations=%s structural_search_rounds=%s "
            "combined_iterations=%s execution_time=%.2fs",
            sum_redesign_iters,
            sum_structural_rounds,
            total_iters,
            total_exec,
        )
        optimization_logger.log(
            state.project_id,
            "ADMIN_AREA_OPTIMIZATION",
            "completed",
            {
                "admin_area_count": len(admin_areas),
                "total_actions": len(all_actions),
                "redesign_iterations_sum": sum_redesign_iters,
                "structural_search_rounds_sum": sum_structural_rounds,
                "combined_iterations": total_iters,
                "execution_time_seconds": total_exec,
            },
        )

        # Solver partition breakdown: store under admin_area_results (area 1) or admin_area_2_results (area 2)
        aa1_payload = admin_area_results if boundary_level == "admin_area_1" else None
        aa2_payload = admin_area_results if boundary_level == "admin_area_2" else None

        result = OptimizationResult(
            actions=all_actions,
            baseline_metrics=self._extract_metrics(state, self.util_evaluator.evaluate(state)),
            final_metrics=final_metrics,
            matrix_3x3=matrix_3x3,
            execution_time_seconds=total_exec,
            converged=True,
            iterations=total_iters,
            redesign_iterations=sum_redesign_iters,
            structural_search_rounds=sum_structural_rounds,
            convergence_reason="Per–admin-area optimization finished for all areas",
            iteration_history=combined_iteration_history,
            admin_area_results=aa1_payload,
            admin_area_2_results=aa2_payload,
        )
        
        return result
    
    def _repair_cross_boundary_referrals(
        self, final_state: NetworkState, boundary_level: str
    ) -> Tuple[List[RedesignAction], dict]:
        """
        Deterministic post-pass: enforce admin boundary on referrals in ``final_state``.

        Mutates ``final_state.referrals`` in place (``final_state`` is a private copy).
        """
        from dno_core.calculators.distance import DistanceCalculator
        from dno_core.models.referral import Referral, SyntheticReferral

        working_days = getattr(self.config, "working_days_per_year", 300) or 300

        # Routing targets are ``state.labs`` (includes facility→lab conversions from this run).
        labs_by_area: Dict[str, List[str]] = {}
        for lab_id, lab in final_state.labs.items():
            area = _effective_admin_area(lab, boundary_level)
            if not area:
                continue
            labs_by_area.setdefault(area, []).append(lab_id)
        for area_labs in labs_by_area.values():
            area_labs.sort()

        load: Dict[str, float] = {}
        for lab_id in final_state.labs:
            load[lab_id] = float(final_state.test_volumes.get(lab_id, 0))
        for ref in final_state.referrals:
            load[ref.lab_id] = load.get(ref.lab_id, 0) + float(
                final_state.test_volumes.get(ref.facility_id, 0)
            )

        cap: Dict[str, float] = {
            lab_id: float(final_state.lab_capacity(lab_id, working_days))
            for lab_id in final_state.labs
        }

        refs_by_facility: Dict[str, List] = {}
        facility_order: List[str] = []
        for ref in final_state.referrals:
            if ref.facility_id not in refs_by_facility:
                refs_by_facility[ref.facility_id] = []
                facility_order.append(ref.facility_id)
            refs_by_facility[ref.facility_id].append(ref)

        new_referrals: List = []
        repair_actions: List[RedesignAction] = []
        repairs_by_area: Dict[str, int] = {}
        unenforceable: Dict[str, int] = {}
        unenforceable_samples: Dict[str, float] = {}
        unknown_area_referrals = 0
        repaired = 0

        def _ref_count(ref) -> int:
            return int(getattr(ref, "referral_count", 0) or 0)

        def _clone_ref(template, lab_id: str, distance_km: float, referral_count: int):
            if isinstance(template, Referral):
                return Referral(
                    facility_id=template.facility_id,
                    lab_id=lab_id,
                    referral_count=referral_count,
                    distance_km=distance_km,
                    is_synthetic=getattr(template, "is_synthetic", False),
                    confidence=getattr(template, "confidence", 1.0),
                )
            return SyntheticReferral(
                facility_id=template.facility_id,
                lab_id=lab_id,
                distance_km=distance_km,
                confidence=getattr(template, "confidence", 0.5),
                is_synthetic=getattr(template, "is_synthetic", True),
                score=getattr(template, "score", 0.0),
            )

        def _select_in_area_lab(facility, facility_area: str, fac_vol: float) -> Optional[str]:
            candidates = labs_by_area.get(facility_area, [])
            if not candidates:
                return None
            fitting: List[tuple] = []
            for lab_id in candidates:
                lab = final_state.labs.get(lab_id)
                if not lab:
                    continue
                projected = load.get(lab_id, 0) + fac_vol
                if projected <= cap.get(lab_id, 0):
                    dist = DistanceCalculator.facility_geodesic_km(facility, lab) or float("inf")
                    fitting.append((dist, lab_id))
            if fitting:
                fitting.sort(key=lambda x: (x[0], x[1]))
                return fitting[0][1]
            best: List[tuple] = []
            for lab_id in candidates:
                lab = final_state.labs.get(lab_id)
                if not lab:
                    continue
                headroom = cap.get(lab_id, 0) - load.get(lab_id, 0)
                dist = DistanceCalculator.facility_geodesic_km(facility, lab) or float("inf")
                best.append((headroom, dist, lab_id))
            best.sort(key=lambda x: (-x[0], x[1], x[2]))
            return best[0][2] if best else None

        for facility_id in facility_order:
            original_refs = refs_by_facility[facility_id]
            facility = final_state.facilities.get(facility_id)
            if not facility:
                new_referrals.extend(original_refs)
                logger.debug(
                    "Boundary repair: facility %s missing from state — keeping %s referral(s)",
                    facility_id,
                    len(original_refs),
                )
                continue

            facility_area = _effective_admin_area(facility, boundary_level)
            if not facility_area:
                # Unknown area is exempt — never force-moved by boundary enforcement.
                new_referrals.extend(original_refs)
                unknown_area_referrals += len(original_refs)
                continue

            fac_vol = float(final_state.test_volumes.get(facility_id, 0))
            violating: List = []
            non_violating: List = []
            for ref in original_refs:
                lab = final_state.labs.get(ref.lab_id)
                if lab and _is_boundary_violation(facility, lab, boundary_level):
                    violating.append(ref)
                else:
                    non_violating.append(ref)

            if not violating:
                new_referrals.extend(original_refs)
                continue

            if non_violating:
                # Case A: facility already has a legitimate in-area assignment — keep it.
                survivor = non_violating[0]
                merged_count = _ref_count(survivor) + sum(_ref_count(r) for r in violating)
                # Survivor lab already counted in load map; only subtract violating sources.
                for vref in violating:
                    load[vref.lab_id] = load.get(vref.lab_id, 0) - fac_vol
                new_lab = final_state.labs.get(survivor.lab_id)
                raw_new_km = (
                    DistanceCalculator.facility_geodesic_km(facility, new_lab) or 0.0
                    if new_lab
                    else survivor.distance_km
                )
                kept_ref = _clone_ref(survivor, survivor.lab_id, raw_new_km, merged_count)
                new_referrals.append(kept_ref)

                first_violating = violating[0]
                old_lab = final_state.labs.get(first_violating.lab_id)
                if old_lab and new_lab:
                    old_dist = DistanceCalculator.facility_distance(facility, old_lab) or 0
                    new_dist = DistanceCalculator.facility_distance(facility, new_lab) or 0
                    fa = _effective_admin_area(facility, boundary_level)
                    la = _effective_admin_area(old_lab, boundary_level)
                    repair_actions.append(
                        RedesignAction(
                            facility_id=facility_id,
                            old_lab_id=first_violating.lab_id,
                            new_lab_id=survivor.lab_id,
                            facility_name=getattr(facility, "name", "") or "",
                            old_lab_name=getattr(old_lab, "name", "") or "",
                            new_lab_name=getattr(new_lab, "name", "") or "",
                            distance_change_km=new_dist - old_dist,
                            new_distance_km=raw_new_km,
                            crosses_admin_boundary=False,
                            admin_boundary_level=boundary_level,
                            rationale=(
                                f"Admin boundary enforcement: facility in {fa} was referred to lab in {la}"
                            ),
                        )
                    )
                repaired += 1
                repairs_by_area[facility_area] = repairs_by_area.get(facility_area, 0) + 1
                continue

            # Case B: all referrals violate — pick nearest in-area lab with capacity headroom.
            chosen_lab_id = _select_in_area_lab(facility, facility_area, fac_vol)
            if not chosen_lab_id:
                # No testing site/lab in this admin area — keep cross-boundary routing.
                new_referrals.extend(original_refs)
                unenforceable[facility_area] = unenforceable.get(facility_area, 0) + len(violating)
                unenforceable_samples[facility_area] = (
                    unenforceable_samples.get(facility_area, 0) + fac_vol
                )
                continue

            for ref in original_refs:
                load[ref.lab_id] = load.get(ref.lab_id, 0) - fac_vol
            load[chosen_lab_id] = load.get(chosen_lab_id, 0) + fac_vol

            template = original_refs[0]
            merged_count = sum(_ref_count(r) for r in original_refs)
            new_lab = final_state.labs.get(chosen_lab_id)
            raw_new_km = (
                DistanceCalculator.facility_geodesic_km(facility, new_lab) or 0.0
                if new_lab
                else 0.0
            )
            new_referrals.append(
                _clone_ref(template, chosen_lab_id, raw_new_km, merged_count)
            )

            first_violating = violating[0]
            old_lab = final_state.labs.get(first_violating.lab_id)
            if old_lab and new_lab:
                old_dist = DistanceCalculator.facility_distance(facility, old_lab) or 0
                new_dist = DistanceCalculator.facility_distance(facility, new_lab) or 0
                fa = _effective_admin_area(facility, boundary_level)
                la = _effective_admin_area(old_lab, boundary_level)
                repair_actions.append(
                    RedesignAction(
                        facility_id=facility_id,
                        old_lab_id=first_violating.lab_id,
                        new_lab_id=chosen_lab_id,
                        facility_name=getattr(facility, "name", "") or "",
                        old_lab_name=getattr(old_lab, "name", "") or "",
                        new_lab_name=getattr(new_lab, "name", "") or "",
                        distance_change_km=new_dist - old_dist,
                        new_distance_km=raw_new_km,
                        crosses_admin_boundary=False,
                        admin_boundary_level=boundary_level,
                        rationale=(
                            f"Admin boundary enforcement: facility in {fa} was referred to lab in {la}"
                        ),
                    )
                )
            repaired += 1
            repairs_by_area[facility_area] = repairs_by_area.get(facility_area, 0) + 1

        final_state.referrals = new_referrals
        final_state._referrals_by_facility = None

        stats = {
            "repaired": repaired,
            "unenforceable": unenforceable,
            "unenforceable_samples": unenforceable_samples,
            "unknown_area_referrals": unknown_area_referrals,
            "repairs_by_area": repairs_by_area,
        }
        return repair_actions, stats
    
    def _filter_state_by_admin_boundary(
        self, state: NetworkState, boundary_value: str, level: str
    ) -> NetworkState:
        """Filter network state to facilities and labs matching ``boundary_value`` on ``level``."""
        import copy
        
        if level not in ("admin_area_1", "admin_area_2"):
            level = "admin_area_1"
        
        filtered_state = copy.deepcopy(state)
        
        # Match on effective admin area (includes legacy state/district fallback).
        # Filter facilities
        filtered_state.facilities = {
            fid: f for fid, f in state.facilities.items()
            if _effective_admin_area(f, level) == boundary_value
        }
        
        # Filter labs
        filtered_state.labs = {
            lid: l for lid, l in state.labs.items()
            if _effective_admin_area(l, level) == boundary_value
        }
        
        # Filter devices (keep only devices at labs in this admin area)
        filtered_state.devices = {
            did: d for did, d in state.devices.items()
            if d.facility_id in filtered_state.labs
        }
        
        # Filter referrals (keep only referrals within this admin area)
        cross_boundary_dropped = 0
        new_refs = []
        for ref in state.referrals:
            if ref.facility_id not in filtered_state.facilities:
                continue
            if ref.lab_id in filtered_state.labs:
                new_refs.append(ref)
            else:
                cross_boundary_dropped += 1
        filtered_state.referrals = new_refs
        
        if cross_boundary_dropped > 0:
            logger.info(
                f"Admin-area filter '{boundary_value}': {cross_boundary_dropped} referral(s) point to labs "
                "outside this area — MILP still compares against full-project assignments via "
                "assignment_reference_state."
            )
        
        return filtered_state
    
    async def _run_optimization(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        milp_assignment_reference_state: Optional[NetworkState] = None,
        milp_boundary_level_override: Optional[str] = None,
    ) -> OptimizationResult:
        """Hierarchical optimization with goal-based and DynamicScenarioConfig support."""
        start_time = time.time()
        started_at = datetime.now()

        # Tighter caps on Bangladesh-scale networks so runs finish sooner.
        cap_config_for_large_network(
            self.config,
            len(state.labs),
            len(state.facilities),
        )
        
        # MILP subgraph boundary: override > Open Referral (OptiDx district) > UI off > UI level.
        _raw_ow, _eff_ow = get_effective_milp_objective_weights(goal)
        if milp_boundary_level_override:
            effective_milp_boundary = milp_boundary_level_override
        elif is_distance_first_open_referral(goal, _eff_ow):
            effective_milp_boundary = "admin_area_1"
        elif not getattr(self.config, "admin_boundary_enabled", False):
            effective_milp_boundary = "admin_area_1"
        else:
            effective_milp_boundary = (
                getattr(self.config, "admin_boundary_level", None) or "admin_area_1"
            )
        effective_milp_boundary, milp_boundary_adaptive_reason = resolve_milp_boundary_level(
            state,
            effective_milp_boundary,
            milp_boundary_level_override,
            max_subgraph_size=int(
                getattr(self.config, "milp_max_subgraph_size", 300) or 300
            ),
        )
        try:
            _cfg_bl = getattr(self.config, "admin_boundary_level", None)
        except Exception:
            _cfg_bl = None
        logger.info(
            "MILP redesign subgraph boundary: effective=%r (milp_override_arg=%r, "
            "config.admin_boundary_level=%r, adaptive_reason=%r)",
            effective_milp_boundary,
            milp_boundary_level_override,
            _cfg_bl,
            milp_boundary_adaptive_reason,
        )
        
        # Initialize result
        all_actions: List[Action] = []
        iteration_history = []
        
        # Extract goal-specific utilization thresholds if available (for baseline evaluation)
        threshold_high = None
        threshold_low = None
        if goal:
            if hasattr(goal, 'max_utilization_target'):
                threshold_high = float(goal.max_utilization_target)
            if hasattr(goal, 'min_utilization_target'):
                threshold_low = float(goal.min_utilization_target)
            elif hasattr(goal, 'utilization_targets') and goal.utilization_targets:
                threshold_high = float(goal.utilization_targets.get('max', None)) if goal.utilization_targets.get('max') else None
                threshold_low = float(goal.utilization_targets.get('min', None)) if goal.utilization_targets.get('min') else None
        
        # Baseline state and metrics (with goal-specific thresholds)
        baseline_state = state
        baseline_util = self.util_evaluator.evaluate(baseline_state, threshold_high=threshold_high, threshold_low=threshold_low)
        baseline_metrics = self._extract_metrics(baseline_state, baseline_util)
        
        # Modes come from DynamicScenarioConfig; default redesign if unset.
        optimization_modes = ["redesign"]
        facility_subset = None
        constraints = None

        if isinstance(goal, OptimizationGoal) or isinstance(goal, DevicePlacementGoal):
            raise ValueError(
                "OSS dno-core supports DynamicScenarioConfig (via OssGoal) only"
            )

        if isinstance(goal, DynamicScenarioConfig):
            optimization_modes = list(goal.optimization_modes or ["redesign"])
            facility_subset = goal.facility_subset
            logger.info(f"Using DynamicScenarioConfig with modes: {optimization_modes}")
            if facility_subset:
                logger.info(f"Partial optimization on {len(facility_subset)} facilities")
            if goal.optimize_by_admin_area:
                logger.info("🌍 ADMIN AREA 1 OPTIMIZATION ENABLED")
                return await self._run_admin_area_wise_optimization(state, goal, progress_callback)
        elif goal is not None:
            raise ValueError(
                f"Unsupported goal type {type(goal).__name__} — use OssGoal / DynamicScenarioConfig"
            )

        allowed_modes = {"redesign"}
        if not set(optimization_modes).issubset(allowed_modes):
            raise ValueError(
                f"OSS optimization_modes must be subset of {allowed_modes}, got {optimization_modes}"
            )
        
        # Budget CapEx ceiling for budget-constrained goals
        self._max_capex_budget: Optional[float] = None
        self._cumulative_capex: float = 0.0
        if constraints is not None and constraints.max_capex is not None:
            self._max_capex_budget = float(constraints.max_capex)
            logger.info("Budget CapEx ceiling: %s", self._max_capex_budget)
        
        # Working state
        current_state = baseline_state.copy()
        previous_metrics = baseline_metrics.copy()
        
        # Track ALL facilities that have been redesigned across iterations
        # This helps with logging but we'll allow re-redesign if beneficial
        all_redesigned_facilities: Set[str] = set()

        # Tabu: prevents ping-pong oscillation where MILP keeps flipping a facility
        # between two nearly-equidistant labs.
        #
        # tabu_labs[facility_id] = set of lab_ids the facility recently left.
        # We keep a 2-iteration rolling window: a facility can't return to any lab
        # it was assigned away from in the last 2 iterations.
        # Near-zero moves (|dist| < 0.5 km) get a permanent lock after first assignment.
        TABU_WINDOW = 3          # iterations to remember (3-cycle patterns observed in logs)
        ZERO_KM_THRESHOLD = 1.0  # km — changes smaller than this are locked permanently
        tabu_labs: Dict[str, Set[str]] = {}   # facility_id → set of blocked lab_ids
        # History: list (one entry per iter) of {facility_id: lab_left}
        tabu_history: List[Dict[str, str]] = []
        # Permanent lock for near-zero moves: facility_id → lab_id locked TO (stable assignment)
        zero_km_locked: Dict[str, str] = {}  # facility_id → lab_id locked TO (stable assignment)
        
        # Optimization loop
        max_iterations = self.config.max_iterations
        converged = False
        convergence_reason = ""
        
        raw_ow, eff_ow = get_effective_milp_objective_weights(goal)
        logger.info(
            "Optimization objective weights (effective for MILP / hybrid scoring): "
            f"utilization={eff_ow.get('utilization', 0):.1%}, "
            f"accessibility(distance)={eff_ow.get('accessibility', 0):.1%}, "
            f"cost={eff_ow.get('cost', 0):.1%}"
            + (f" | raw request: {raw_ow}" if raw_ow else " | raw: (none → defaults)")
        )
        
        # Log optimization start
        optimization_logger.log(
            state.project_id,
            "OPTIMIZATION_START",
            "started",
            {
                "modes": optimization_modes,
                "goal_type": type(goal).__name__ if goal else "None",
                "facility_count": len(state.facilities),
                "lab_count": len(state.labs),
                "device_count": len(state.devices),
                "max_iterations": max_iterations,
                "objective_weights_raw": raw_ow,
                "objective_weights_effective": eff_ow,
                "objective_weights_summary": {
                    "utilization_percent": round(eff_ow.get("utilization", 0) * 100, 2),
                    "accessibility_percent": round(eff_ow.get("accessibility", 0) * 100, 2),
                    "cost_percent": round(eff_ow.get("cost", 0) * 100, 2),
                },
                "milp_boundary_effective": effective_milp_boundary,
                "milp_boundary_override_arg": milp_boundary_level_override,
                "milp_boundary_adaptive_reason": milp_boundary_adaptive_reason,
                "config_admin_boundary_level": _cfg_bl,
            }
        )
        
        # Phase 1: redesign iterations (reassign HF→lab referrals).
        redesign_iteration_count = 0
        structural_rounds = 0

        # PHASE 1: REDESIGN ITERATIONS (Optimize demand distribution)
        redesign_enabled = "redesign" in optimization_modes
        
        if redesign_enabled:
            logger.info("=" * 80)
            logger.info("PHASE 1: REDESIGN ITERATIONS (Optimizing demand distribution)")
            logger.info("=" * 80)

            iteration = 0
            for iteration in range(1, max_iterations + 1):
                logger.info(f"=== Redesign Iteration {iteration}/{max_iterations} ===")
                
                optimization_logger.log(
                    state.project_id,
                    "ITERATION",
                    "started",
                    {
                        "iteration": iteration,
                        "max_iterations": max_iterations,
                        "phase": "redesign",
                        "total_actions_so_far": len(all_actions)
                    },
                    iteration=iteration
                )
                
                if progress_callback:
                    progress_callback(f"Redesign Iteration {iteration}", iteration, max_iterations)
                
                iteration_actions: List[Action] = []
                
                # Extract goal-specific utilization thresholds
                threshold_high = None
                threshold_low = None
                if goal:
                    if hasattr(goal, 'max_utilization_target'):
                        threshold_high = float(goal.max_utilization_target)
                    if hasattr(goal, 'min_utilization_target'):
                        threshold_low = float(goal.min_utilization_target)
                    elif hasattr(goal, 'utilization_targets') and goal.utilization_targets:
                        threshold_high = float(goal.utilization_targets.get('max', None)) if goal.utilization_targets.get('max') else None
                        threshold_low = float(goal.utilization_targets.get('min', None)) if goal.utilization_targets.get('min') else None
                
                # Redesign (MILP) - optimize referral patterns
                optimization_logger.log(
                    state.project_id,
                    "REDESIGN",
                    "started",
                    {
                        "iteration": iteration,
                        "objective_weights_effective": eff_ow,
                        "objective_weights_summary": {
                            "utilization_percent": round(eff_ow.get("utilization", 0) * 100, 2),
                            "accessibility_percent": round(eff_ow.get("accessibility", 0) * 100, 2),
                            "cost_percent": round(eff_ow.get("cost", 0) * 100, 2),
                        },
                    },
                    iteration=iteration,
                )
                
                redesigns = await self.milp_solver.solve_redesign(
                    current_state,
                    goal,
                    assignment_reference_state=(
                        milp_assignment_reference_state or current_state
                    ),
                    boundary_level_override=effective_milp_boundary,
                )
                logger.info(f"Redesign solver found {len(redesigns)} redesign candidates")
                
                if len(redesigns) > 0:
                    logger.info(f"  Candidates: {[(r.facility_id[:8], state.facilities.get(r.facility_id).name if state.facilities.get(r.facility_id) else 'unknown') for r in redesigns[:5]]}")
                
                # ── Tabu / oscillation filter ─────────────────────────────
                # Two layers of protection:
                # 1. Rolling 2-iteration window: can't return to any lab left in
                #    the last TABU_WINDOW iterations.
                # 2. Near-zero permanent lock: if a move is < ZERO_KM_THRESHOLD,
                #    the facility keeps its current assignment permanently
                #    (the MILP can find no meaningful improvement there).
                iter_tabu_record: Dict[str, str] = {}  # facility_id → old_lab_id this iter
                filtered_redesigns = []
                tabu_blocked = 0
                zero_blocked = 0
                for r in redesigns:
                    fid = r.facility_id
                    fac_name = getattr(current_state.facilities.get(fid), "name", fid[:8])
                    new_lab_name = getattr(current_state.labs.get(r.new_lab_id), "name", r.new_lab_id[:8])
                    dist_chg = float(r.distance_change_km)

                    # Layer 2: near-zero permanent lock
                    if abs(dist_chg) < ZERO_KM_THRESHOLD and fid in zero_km_locked:
                        zero_blocked += 1
                        logger.info(
                            f"🔒 Zero-km lock: {fac_name} stays at {zero_km_locked[fid][:25]} "
                            f"(attempted {dist_chg:+.4f} km move)"
                        )
                        continue

                    # Layer 1: rolling tabu window
                    blocked_labs = tabu_labs.get(fid, set())
                    if r.new_lab_id in blocked_labs:
                        tabu_blocked += 1
                        logger.info(
                            f"🚫 Tabu block: {fac_name} → {new_lab_name} "
                            f"(returned to a recently-left lab, {dist_chg:+.3f} km)"
                        )
                        continue

                    filtered_redesigns.append(r)
                    iter_tabu_record[fid] = r.old_lab_id

                    # Register as near-zero permanent lock
                    if abs(dist_chg) < ZERO_KM_THRESHOLD:
                        zero_km_locked[fid] = r.new_lab_id
                        logger.info(
                            f"🔒 Locking {fac_name} → {new_lab_name} permanently "
                            f"(near-zero move, {dist_chg:+.4f} km)"
                        )

                # Advance tabu window
                tabu_history.append(iter_tabu_record)
                if len(tabu_history) > TABU_WINDOW:
                    tabu_history.pop(0)
                # Rebuild tabu_labs from rolling window
                tabu_labs = {}
                for record in tabu_history:
                    for fid, old_lab in record.items():
                        tabu_labs.setdefault(fid, set()).add(old_lab)

                if tabu_blocked or zero_blocked:
                    logger.info(
                        f"  Tabu/lock removed {tabu_blocked} oscillating + {zero_blocked} zero-km "
                        f"proposals; {len(filtered_redesigns)} remain"
                    )
                redesigns = filtered_redesigns

                # Apply redesign actions
                # Track which facilities are being re-redesigned
                re_redesigned_count = 0
                for redesign in redesigns:
                    if redesign.facility_id in all_redesigned_facilities:
                        re_redesigned_count += 1
                        facility = state.facilities.get(redesign.facility_id)
                        logger.info(f"♻️  Re-redesigning {facility.name if facility else redesign.facility_id[:8]} (previously redesigned, but MILP found better assignment)")
                    
                    current_state = current_state.with_referral_changed(
                        redesign.facility_id,
                        redesign.new_lab_id
                    )
                    iteration_actions.append(redesign)
                    all_redesigned_facilities.add(redesign.facility_id)
                
                if re_redesigned_count > 0:
                    logger.info(f"♻️  Re-redesigned {re_redesigned_count}/{len(redesigns)} facilities (allowing iterative improvement)")
                
                optimization_logger.log(
                    state.project_id,
                    "REDESIGN",
                    "completed",
                    {
                        "iteration": iteration,
                        "actions_found": len(redesigns),
                        "redesigns": [
                            {
                                "facility_id": r.facility_id,
                                "facility_name": getattr(current_state.facilities.get(r.facility_id), "name", r.facility_id[:8]),
                                "old_lab_id": r.old_lab_id,
                                "old_lab_name": getattr(current_state.labs.get(r.old_lab_id), "name", r.old_lab_id[:8]),
                                "new_lab_id": r.new_lab_id,
                                "new_lab_name": getattr(current_state.labs.get(r.new_lab_id), "name", r.new_lab_id[:8]),
                                "distance_change_km": round(r.distance_change_km, 3),
                                "facility_volume": current_state.test_volumes.get(r.facility_id, 0),
                            }
                            for r in redesigns
                        ],
                    },
                    iteration=iteration
                )
                
                if redesigns:
                    util_report = self.util_evaluator.evaluate(current_state, threshold_high=threshold_high, threshold_low=threshold_low)
                    logger.info(f"After {len(redesigns)} redesigns: overall utilization = {util_report.overall_utilization:.1%}")
                    
                    # Structured per-lab utilization snapshot after this iteration
                    _iter_lab_utils = self.milp_solver._calculate_utilizations(current_state)
                    _iter_lab_util_rows = []
                    for _lid, _lab in current_state.labs.items():
                        _u = _iter_lab_utils.get(_lid, 0.0)
                        _cap = current_state.lab_capacity(_lid, self.config.working_days_per_year)
                        _base_demand = float(current_state.test_volumes.get(_lid, 0))
                        _referral_demand = sum(
                            float(current_state.test_volumes.get(ref.facility_id, 0))
                            for ref in current_state.referrals
                            if ref.lab_id == _lid
                        )
                        _iter_lab_util_rows.append({
                            "lab_id": _lid,
                            "lab_name": getattr(_lab, "name", _lid[:8]),
                            "utilization": round(_u, 4),
                            "capacity": round(_cap, 0),
                            "base_demand": round(_base_demand, 0),
                            "referral_demand": round(_referral_demand, 0),
                            "total_demand": round(_base_demand + _referral_demand, 0),
                            "overutilized": _u > 1.0,
                        })
                    _iter_overutil = [r for r in _iter_lab_util_rows if r["overutilized"]]
                    optimization_logger.log(
                        state.project_id,
                        "REDESIGN_LAB_UTILIZATION",
                        "snapshot",
                        {
                            "iteration": iteration,
                            "overall_utilization": round(util_report.overall_utilization, 4),
                            "overutilized_count": len(_iter_overutil),
                            "overutilized_labs": _iter_overutil,
                            "all_labs": _iter_lab_util_rows,
                        },
                        iteration=iteration
                    )
                    
                    # Log distance changes for each redesign
                    logger.info(f"📏 Distance changes this iteration:")
                    for redesign in redesigns[:10]:
                        facility = state.facilities.get(redesign.facility_id)
                        old_lab = state.labs.get(redesign.old_lab_id)
                        new_lab = state.labs.get(redesign.new_lab_id)
                        logger.info(f"   {facility.name if facility else redesign.facility_id[:8]}: {old_lab.name if old_lab else redesign.old_lab_id[:8]} → {new_lab.name if new_lab else redesign.new_lab_id[:8]} ({redesign.distance_change_km:+.1f}km)")
                    
                    # Track accessibility impact after redesigns
                    from dno_core.evaluators.accessibility import AccessibilityEvaluator
                    access_eval_temp = AccessibilityEvaluator(self.config)
                    access_eval_temp.baseline_score = None
                    baseline_access = access_eval_temp.evaluate(baseline_state)
                    access_eval_temp.baseline_score = baseline_access["population_weighted_accessibility"]
                    current_access = access_eval_temp.evaluate(current_state)
                    
                    access_change = current_access["accessibility_improvement_pct"]
                    logger.info(f"  Accessibility change: {access_change:+.2f}%")
                    if access_change < 0:
                        logger.warning(f"  ⚠️  Accessibility worsened by {abs(access_change):.2f}% after redesigns")
                
                all_actions.extend(iteration_actions)
                
                logger.info(f"Redesign iteration {iteration}: {len(redesigns)} redesigns, total actions: {len(all_actions)}")
                
                # Calculate metrics
                current_util = self.util_evaluator.evaluate(current_state)
                current_metrics = self._extract_metrics(current_state, current_util)
                
                # Record iteration
                iteration_history.append({
                    "iteration": iteration,
                    "phase": "redesign",
                    "actions": len(iteration_actions),
                    "redesigns": len(redesigns),
                    "utilization": current_metrics["overall_utilization"],
                    "overutilized_count": current_metrics.get("overutilized_count", 0),
                    "underutilized_count": current_metrics.get("underutilized_count", 0),
                })
                
                # Check convergence (redesign-specific)
                converged, convergence_reason = self._check_convergence(
                    current_metrics,
                    previous_metrics,
                    iteration,
                    len(iteration_actions),
                    goal,
                    iteration_history=iteration_history,
                )
                
                optimization_logger.log(
                    state.project_id,
                    "CONVERGENCE_CHECK",
                    "completed",
                    {
                        "converged": converged,
                        "reason": convergence_reason,
                        "metrics": current_metrics,
                        "actions_this_iteration": len(iteration_actions)
                    },
                    iteration=iteration
                )
                
                if converged:
                    logger.info(f"Redesign converged at iteration {iteration}: {convergence_reason}")
                    break
                
                previous_metrics = current_metrics
            
            redesign_iteration_count = iteration
            if not converged:
                convergence_reason = f"Redesign: Max iterations ({max_iterations}) reached"
                logger.info(convergence_reason)
        else:
            logger.info("Redesign skipped: not in optimization_modes")
            iteration = 0
            redesign_iteration_count = 0
            converged = True
            convergence_reason = "No redesign needed"
        
        if not converged:
            convergence_reason = f"Max iterations ({max_iterations}) reached"

        # Use current_state as projected state - expansions are now applied after iterations
        # We only need to apply relocations/redesigns for final projection
        projected_state = self._apply_non_expansion_actions(current_state, all_actions)

        # Global runs: repair cross-boundary referrals when project config enforces boundaries.
        if _admin_boundaries_enforced(self.config):
            repair_actions, repair_stats = self._repair_cross_boundary_referrals(
                projected_state, effective_milp_boundary
            )
            if repair_actions:
                all_actions.extend(repair_actions)
                logger.info(
                    "Boundary repair (global run): %d referral(s) repaired, unenforceable=%s",
                    repair_stats.get("repaired", 0),
                    repair_stats.get("unenforceable"),
                )
        
        # Final evaluation on projected state
        if progress_callback:
            progress_callback("Final Evaluation", max_iterations, max_iterations)
        
        final_util = self.util_evaluator.evaluate(projected_state)
        final_metrics = self._extract_metrics(projected_state, final_util)
        
        # 3×3 Matrix evaluation
        matrix_3x3 = self.matrix_evaluator.evaluate(
            baseline_state,
            projected_state,  # Use projected state
            all_actions,
            goal
        )
        
        # Deduplicate actions to show only net final changes
        all_actions = self._deduplicate_actions(all_actions)
        
        # Populate names for display in frontend (resolving UUIDs)
        # using baseline_state which contains all facilities and labs
        facility_lookup = {f.id: f.name for f in baseline_state.all_facilities()}
        
        # Enrich metrics with lab-level details so consumers can use result directly without re-evaluating
        baseline_metrics = self._enrich_metrics_with_lab_details(
            baseline_metrics, baseline_util, facility_lookup, "baseline"
        )
        final_metrics = self._enrich_metrics_with_lab_details(
            final_metrics, final_util, facility_lookup, "final"
        )
        
        for action in all_actions:
            if isinstance(action, RelocationAction):
                if not action.source_lab_name:
                    action.source_lab_name = facility_lookup.get(action.source_lab_id, action.source_lab_id)
                if not action.target_lab_name:
                    action.target_lab_name = facility_lookup.get(action.target_lab_id, action.target_lab_id)
            
            elif isinstance(action, RedesignAction):
                if not action.facility_name:
                    action.facility_name = facility_lookup.get(action.facility_id, action.facility_id)
                if not action.old_lab_name:
                    action.old_lab_name = facility_lookup.get(action.old_lab_id, action.old_lab_id)
                if not action.new_lab_name:
                    action.new_lab_name = facility_lookup.get(action.new_lab_id, action.new_lab_id)
            
            elif isinstance(action, ExpansionAction):
                if not action.lab_name:
                    action.lab_name = facility_lookup.get(action.lab_id, action.lab_id)
            
            elif isinstance(action, FacilityConversionAction):
                if not action.facility_name:
                    action.facility_name = facility_lookup.get(action.facility_id, action.facility_id)
        
        # Build result
        execution_time = time.time() - start_time
        
        # Detect impossibility for utilization balance goals
        impossibility_detected = False
        impossibility_reason = None
        remaining_issues = None
        
        if goal and hasattr(goal, 'goal_type') and goal.goal_type == 'utilization_balance':
            # Check if we still have overutilization after all efforts
            overutil_count = final_metrics.get("overutilized_count", 0)
            underutil_count = final_metrics.get("underutilized_count", 0)
            
            if overutil_count > 0 or underutil_count > 0:
                # Goal not fully achieved - determine if it's impossible or just incomplete
                if len(all_actions) == 0:
                    impossibility_detected = True
                    impossibility_reason = "No valid actions found to balance utilization. Network may require capacity expansion or infrastructure changes."
                elif converged and convergence_reason and "No further actions" in convergence_reason:
                    impossibility_detected = True
                    impossibility_reason = f"Optimization exhausted all available options. {overutil_count} labs remain overutilized, {underutil_count} remain underutilized. This may require adding new capacity or relaxing distance/infrastructure constraints."
                else:
                    impossibility_reason = f"Partial success: {len(all_actions)} actions taken, but {overutil_count} labs remain overutilized, {underutil_count} remain underutilized."
                
                remaining_issues = {
                    "overutilized_labs": overutil_count,
                    "underutilized_labs": underutil_count,
                    "total_actions_attempted": len(all_actions),
                    "iterations_completed": redesign_iteration_count + structural_rounds,
                }

        if goal and getattr(goal, "goal_type", None) == "device_placement":
            imp = matrix_3x3.impact
            remaining_issues = {
                "requested_device_count": imp.placement_requested_count,
                "placed_device_count": imp.placement_placed_count,
                "remaining_device_quota": imp.remaining_device_quota,
                "placement_stop_reason": getattr(goal, "_placement_stop_reason", None),
                "blocked_reason": imp.placement_blocked_reason,
            }
            if imp.placement_placed_count == 0 and isinstance(goal, DevicePlacementGoal) and goal.has_actionable_spec():
                impossibility_detected = True
                impossibility_reason = (
                    imp.placement_blocked_reason
                    or "No devices placed — no feasible or beneficial site found"
                )
        
        total_work_iterations = redesign_iteration_count + structural_rounds
        result = OptimizationResult(
            started_at=started_at,
            completed_at=datetime.now(),
            execution_time_seconds=execution_time,
            iterations=total_work_iterations,
            redesign_iterations=redesign_iteration_count,
            structural_search_rounds=structural_rounds,
            converged=converged,
            convergence_reason=convergence_reason,
            actions=all_actions,
            baseline_metrics=baseline_metrics,
            final_metrics=final_metrics,
            matrix_3x3=matrix_3x3,
            goal_type=getattr(goal, 'goal_type', None) if goal else None,
            goal_achieved=matrix_3x3.impact.goal_met if goal else False,
            impossibility_detected=impossibility_detected,
            impossibility_reason=impossibility_reason,
            remaining_issues=remaining_issues,
            iteration_history=iteration_history,
        )
        
        logger.info(
            f"Optimization completed: {len(all_actions)} actions, "
            f"{total_work_iterations} work-iterations "
            f"(redesign={redesign_iteration_count}, structural_search={structural_rounds}), "
            f"{execution_time:.2f}s"
        )
        
        optimization_logger.log(
            state.project_id,
            "OPTIMIZATION_COMPLETE",
            "completed",
            {
                "total_iterations": total_work_iterations,
                "redesign_iterations": redesign_iteration_count,
                "structural_search_rounds": structural_rounds,
                "total_actions": len(all_actions),
                "execution_time": execution_time,
                "converged": converged,
                "final_metrics": final_metrics
            }
        )
        
        return result

    def _apply_action_to_state(self, state: NetworkState, action: Action) -> NetworkState:
        """Apply a RedesignAction to state (OSS scope)."""
        if isinstance(action, RedesignAction):
            return state.with_referral_changed(action.facility_id, action.new_lab_id)
        return state
    
    def _extract_metrics(
        self, 
        state: NetworkState, 
        util_report: UtilizationReport
    ) -> dict:
        """Extract key metrics from state."""        
        working_days = self.config.working_days_per_year
        return {
            "total_capacity": state.total_network_capacity(working_days),
            "total_demand": state.total_network_demand(),
            "overall_utilization": util_report.overall_utilization,
            "overutilized_count": len(util_report.overutilized_labs),
            "underutilized_count": len(util_report.underutilized_labs),
            "device_count": len(state.devices),
            "lab_count": len(state.labs),
        }
    
    def _enrich_metrics_with_lab_details(
        self,
        metrics: dict,
        util_report: UtilizationReport,
        facility_lookup: dict,
        prefix: str
    ) -> dict:
        """Add lab-level details to metrics so consumers can use result directly without re-evaluating."""
        out = dict(metrics)
        overutilized_details = []
        for lab_id in util_report.overutilized_labs:
            util = util_report.lab_utilizations.get(lab_id, 0)
            overutilized_details.append({
                "lab_id": lab_id,
                "lab_name": facility_lookup.get(lab_id, lab_id[:8] if lab_id else ""),
                "utilization_percent": util * 100,
                "demand": util_report.lab_demands.get(lab_id, 0),
                "capacity": util_report.lab_capacities.get(lab_id, 0)
            })
        out["overutilized_lab_details"] = overutilized_details
        if prefix == "baseline":
            out["baseline_overutilized_lab_details"] = overutilized_details
        out["utilization_distribution"] = {
            "overutilized": len(util_report.overutilized_labs),
            "balanced": len(util_report.balanced_labs),
            "underutilized": len(util_report.underutilized_labs)
        }
        # Aliases for consumer compatibility
        out["overutilized_labs"] = len(util_report.overutilized_labs)
        out["underutilized_labs"] = len(util_report.underutilized_labs)
        out["balanced_labs"] = len(util_report.balanced_labs)
        out["capacity"] = out.get("total_capacity", 0)
        out["utilization"] = out.get("overall_utilization", 0)
        return out
    
    def _apply_non_expansion_actions(
        self,
        state: NetworkState,
        actions: List
    ) -> NetworkState:
        """Apply redesign actions only — OSS projected state."""
        import copy
        from dno_core.models.actions import RedesignAction

        projected = copy.deepcopy(state)

        for action in actions:
            if isinstance(action, RedesignAction):
                projected = projected.with_referral_changed(
                    action.facility_id,
                    action.new_lab_id
                )

        return projected
    
    def _check_convergence(
        self,
        current_metrics: dict,
        previous_metrics: dict,
        iteration: int,
        actions_this_iteration: int,
        goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]] = None,
        iteration_history: Optional[List[Dict]] = None
    ) -> tuple[bool, str]:
        """
        Check if optimization has converged with improved early success detection.
        
        Criteria:
        - Early success detection (can converge before min iterations if goals met)
        - Minimum iterations for stability (configurable, default 3)
        - Multiple metrics must stabilize:
          * Utilization change < tolerance (configurable)
          * Overutilized lab count unchanged
          * Underutilized lab count unchanged
        - Goal-specific early termination
        - Oscillation detection (bouncing between similar states)
        """
        # Configurable thresholds (can be moved to config)
        # Use a lower minimum when there is no overutilization — no need to keep exploring
        base_min = getattr(self.config, 'min_convergence_iterations', 8)
        has_overutil = current_metrics.get("overutilized_count", 0) > 0
        MIN_ITERATIONS = base_min if has_overutil else min(base_min, 4)
        EARLY_SUCCESS_MIN_ITERATIONS = 3  # Allow early success after 3 clean iterations
        TOLERANCE = getattr(self.config, 'convergence_tolerance', 0.01)
        UTILIZATION_TARGET_MAX = float(getattr(self.config, "utilization_threshold_high", 1.0))
        OSCILLATION_WINDOW = getattr(self.config, 'oscillation_detection_window', 4)
        
        # Early success detection (before minimum iterations)
        # Only allow early success if we've actually taken some actions OR if we've run enough iterations
        if iteration >= EARLY_SUCCESS_MIN_ITERATIONS:
            # Check if overutilization completely resolved (early success)
            if current_metrics["overutilized_count"] == 0:
                if previous_metrics["overutilized_count"] > 0:
                    logger.info(f"✓ Early success: All overutilization resolved at iteration {iteration}!")
                    logger.info(f"  Previous overutilized: {previous_metrics['overutilized_count']}, Current: 0")
                    return True, f"All overutilization resolved at iteration {iteration}"
                elif current_metrics["overutilized_count"] == 0 and current_metrics.get("overall_utilization", 0) < UTILIZATION_TARGET_MAX:
                    # No overutilization — check if utilization is stable across last 3 iters in history
                    if iteration_history and len(iteration_history) >= 3:
                        last3_util = [h.get("utilization", 0) for h in iteration_history[-3:]]
                        if max(last3_util) - min(last3_util) < TOLERANCE:
                            logger.info(f"✓ Early success: No overutilization, utilization stable ({last3_util}) after {iteration} iterations")
                            return True, f"No overutilization and utilization stable at iteration {iteration}"
                    # Fallback: zero actions after minimum iterations
                    if actions_this_iteration == 0 and iteration >= MIN_ITERATIONS:
                        logger.info(f"✓ Early success: No overutilization and no actions needed after {iteration} iterations")
                        logger.info(f"  Overall utilization: {current_metrics.get('overall_utilization', 0):.1%} < {UTILIZATION_TARGET_MAX:.1%}")
                        return True, f"Optimal state reached at iteration {iteration} (no improvements found)"
            else:
                logger.debug(f"Early success check failed: {current_metrics['overutilized_count']} labs still overutilized (iteration {iteration})")
            
            # Goal-specific early success checks
            if goal:
                from dno_core.models.goals import UtilizationBalanceGoal, CapacityGoal
                
                # Utilization balance goal: check if targets are met
                if isinstance(goal, UtilizationBalanceGoal):
                    current_util = current_metrics.get("overall_utilization", 0)
                    max_target = goal.max_utilization_target
                    min_target = goal.min_utilization_target
                    
                    if (current_util <= max_target and 
                        current_metrics["overutilized_count"] == 0 and
                        current_metrics.get("underutilized_count", 0) == 0):
                        logger.info(f"✓ Early success: Utilization balance goal met (util={current_util:.2%}, target={min_target:.2%}-{max_target:.2%})")
                        return True, f"Utilization balance goal achieved at iteration {iteration}"
                
                # Capacity goal: check if capacity target is met
                if isinstance(goal, CapacityGoal):
                    current_capacity = current_metrics.get("total_capacity", 0)
                    if current_capacity >= goal.target_annual_tests:
                        logger.info(f"✓ Early success: Capacity goal met ({current_capacity:,} >= {goal.target_annual_tests:,})")
                        return True, f"Capacity goal achieved at iteration {iteration}"
        
        # Detect oscillation pattern (if we have enough history)
        if iteration_history and len(iteration_history) >= OSCILLATION_WINDOW:
            window = iteration_history[-OSCILLATION_WINDOW:]
            recent_overutil = [hist.get("overutilized_count", 0) for hist in window]
            recent_actions = [hist.get("actions", 0) for hist in window]
            unique_overutil = len(set(recent_overutil))
            overutil_range = max(recent_overutil) - min(recent_overutil)
            # Oscillating on utilization state
            if unique_overutil <= 2 and overutil_range <= 1:
                logger.warning(f"Oscillation detected: overutilized_count in {recent_overutil}, stopping.")
                return True, f"Oscillation detected at iteration {iteration} (overutilized: {recent_overutil})"
            # Also detect action-count oscillation: same non-zero action counts repeating
            if len(set(recent_actions)) <= 2 and all(a > 0 for a in recent_actions) and max(recent_actions) - min(recent_actions) <= 5:
                logger.warning(f"Action-count oscillation detected: {recent_actions}, stopping.")
                return True, f"Action oscillation at iteration {iteration} (actions per iter: {recent_actions})"
        
        # Check if metrics stable for 3 consecutive iterations (using iteration history)
        if iteration_history and iteration >= max(MIN_ITERATIONS, 3):
            last_3_hist = iteration_history[-3:]
            if len(last_3_hist) >= 3:
                last_3_util = [hist.get("utilization", 0) for hist in last_3_hist]
                last_3_overutil = [hist.get("overutilized_count", 0) for hist in last_3_hist]
                
                util_range = max(last_3_util) - min(last_3_util) if last_3_util else 0
                overutil_stable = len(set(last_3_overutil)) == 1
                
                if util_range < TOLERANCE and overutil_stable:
                    logger.info(f"Metrics stabilized: utilization range={util_range:.4f}, overutilized_count={last_3_overutil[0]}")
                    return True, f"Metrics stabilized at iteration {iteration}"
        
        # Require minimum iterations for stability checks
        if iteration < MIN_ITERATIONS:
            return False, ""
        
        # No actions = converged (but only after min iterations)
        if actions_this_iteration == 0:
            return True, f"No further actions available at iteration {iteration}"
        
        # Calculate metric deltas
        util_change = abs(
            current_metrics["overall_utilization"] - 
            previous_metrics["overall_utilization"]
        )
        overutil_change = abs(
            current_metrics["overutilized_count"] - 
            previous_metrics["overutilized_count"]
        )
        underutil_change = abs(
            current_metrics["underutilized_count"] - 
            previous_metrics["underutilized_count"]
        )
        
        # Log convergence metrics
        logger.info(f"Convergence check at iteration {iteration}:")
        logger.info(f"  Utilization change: {util_change:.4f} (tolerance: {TOLERANCE})")
        logger.info(f"  Overutilized change: {overutil_change}")
        logger.info(f"  Underutilized change: {underutil_change}")
        logger.info(f"  Actions this iteration: {actions_this_iteration}")
        
        # Check if ALL metrics are stable
        util_stable = util_change < TOLERANCE
        overutil_stable = overutil_change == 0
        underutil_stable = underutil_change == 0
        
        # Only converge on stability if we've had NO actions for 2 consecutive iterations
        # This prevents premature convergence when actions are small but still happening
        if util_stable and overutil_stable and underutil_stable:
            if actions_this_iteration == 0:
                return True, f"All metrics stabilized (util Δ={util_change:.4f}, overutil Δ={overutil_change}, underutil Δ={underutil_change})"
            else:
                logger.info(f"Metrics stable but actions still happening ({actions_this_iteration} actions), continuing...")
                return False, ""
        
        return False, ""
    
    def _deduplicate_actions(self, actions: List[Action]) -> List[Action]:
        """
        Deduplicate actions to show only net final changes.
        
        For redesigns: Keep only the last action per facility.
        For relocations: Keep only the last action per device.
        """
        # Redesigns: facility_id -> last action
        redesign_map = {}
        relocations = []
        expansions = []
        conversions = []
        shift_map: Dict[str, AddShiftAction] = {}
        integration_map: Dict[Tuple[str, str], TestIntegrationAction] = {}
        
        for action in actions:
            if isinstance(action, RedesignAction):
                prev = redesign_map.get(action.facility_id)
                if prev is not None:
                    # Net change vs baseline: keep first hop's old lab, sum distance deltas.
                    action.old_lab_id = prev.old_lab_id
                    if getattr(prev, "old_lab_name", ""):
                        action.old_lab_name = prev.old_lab_name
                    action.distance_change_km = float(
                        getattr(prev, "distance_change_km", 0) or 0
                    ) + float(getattr(action, "distance_change_km", 0) or 0)
                redesign_map[action.facility_id] = action
            elif isinstance(action, RelocationAction):
                relocations.append(action)
            elif isinstance(action, ExpansionAction):
                expansions.append(action)
            elif isinstance(action, FacilityConversionAction):
                conversions.append(action)
            elif isinstance(action, AddShiftAction):
                shift_map[action.device_id] = action
            elif isinstance(action, TestIntegrationAction):
                integration_map[(action.device_id, action.test_type_id)] = action
        
        # Combine deduplicated actions
        final_actions = (
            list(redesign_map.values())
            + relocations
            + list(integration_map.values())
            + list(shift_map.values())
            + expansions
            + conversions
        )
        
        logger.info(f"Deduplicated actions: {len(actions)} → {len(final_actions)}")
        return final_actions
