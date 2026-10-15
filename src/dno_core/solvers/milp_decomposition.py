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
MILP decomposition solver with heuristic-driven subgraph extraction.
"""

from typing import List, Dict, Optional, Set, Union, Tuple, Any
from dataclasses import dataclass, field, replace
from decimal import Decimal
from collections import defaultdict
import asyncio
import os
import shutil
import logging

from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.facility import Facility
from dno_core.models.actions import RedesignAction
from dno_core.models.goals import OptimizationGoal, DynamicScenarioConfig
from dno_core.calculators.distance import DistanceCalculator
from dno_core.logging.progress import optimization_logger
from dno_core.models.referral import resolve_sample_transport_cpk

logger = logging.getLogger(__name__)


async def _solve_prob_in_executor(prob, solver, semaphore: "asyncio.Semaphore") -> None:
    """
    Run a blocking CBC solve off the event loop so sibling subgraph solves can run
    concurrently. CBC itself is a single-threaded subprocess, so the semaphore caps how
    many run at once (≈ CPU cores) to avoid oversubscribing small VMs. This changes only
    *where* the solve runs — the model, objective and solution are unchanged.
    """
    loop = asyncio.get_event_loop()
    async with semaphore:
        await loop.run_in_executor(None, prob.solve, solver)


def _default_parallel_solves(config) -> int:
    """Bounded MILP solve concurrency: min(configured cap, available CPU cores)."""
    configured = int(getattr(config, "milp_max_parallel_solves", 4) or 4)
    cores = os.cpu_count() or 4
    return max(1, min(configured, cores))


_CBC_PATH_ENV_VAR = "DNO_CBC_PATH"


def _resolve_cbc_solver(*, timeLimit, msg, threads):
    """
    Build a CBC solver instance that actually runs on this host.

    PuLP's bundled PULP_CBC_CMD ships a precompiled x86_64 binary. On Apple
    Silicon Macs without Rosetta 2 (and other hosts where the bundled binary's
    architecture doesn't match the interpreter) it raises
    "OSError: Bad CPU type in executable" even though `available()` reports
    the solver as usable (it only checks file permissions, not architecture).
    Prefer a CBC binary from PATH (e.g. `brew install cbc` on macOS,
    `apt install coinor-cbc` on Linux), overridable via DNO_CBC_PATH, and fall
    back to the bundled solver otherwise.
    """
    import pulp

    custom_path = os.environ.get(_CBC_PATH_ENV_VAR) or shutil.which("cbc")
    if custom_path:
        return pulp.COIN_CMD(
            path=custom_path, mip=True, msg=msg, timeLimit=timeLimit, threads=threads
        )
    return pulp.PULP_CBC_CMD(timeLimit=timeLimit, msg=msg, threads=threads)


def _cbc_threads_per_solve(config, num_subgraphs_in_batch: int) -> int:
    """
    Split the CBC CPU budget across concurrent subgraph solves.
    Uses milp_max_parallel_solves as total thread budget (capped by cores).
    """
    parallel_budget = _default_parallel_solves(config)
    active_solves = max(1, min(int(num_subgraphs_in_batch or 1), parallel_budget))
    return max(1, parallel_budget // active_solves)


def validate_and_normalize_weights(
    objective_weights: Optional[Dict[str, float]],
    default_weights: Optional[Dict[str, float]] = None,
    context: str = "optimization"
) -> Optional[Dict[str, float]]:
    """
    Validate and normalize objective weights for multi-objective optimization.
    
    Args:
        objective_weights: User-provided weights dictionary
        default_weights: Default weights if normalization needed (optional)
        context: Context string for logging (e.g., "MILP", "relocation", "expansion")
        
    Returns:
        Normalized weights dict, or default_weights copy when input is missing/empty
        (if default_weights provided), or None if invalid and no defaults available.
    """
    if not objective_weights:
        if default_weights:
            logger.info(f"{context}: No objective weights provided, using default weights")
            return default_weights.copy()
        return None
    
    # Check for negative weights
    if any(w < 0 for w in objective_weights.values()):
        logger.warning(f"{context}: Negative weights detected, setting to 0")
        objective_weights = {k: max(0, v) for k, v in objective_weights.items()}
    
    # Calculate sum
    weight_sum = sum(objective_weights.values())
    
    # If sum is 0, use defaults or return None
    if weight_sum == 0:
        if default_weights:
            logger.warning(f"{context}: All weights are 0, using default weights")
            return default_weights.copy()
        else:
            logger.warning(f"{context}: All weights are 0, returning None")
            return None
    
    # Normalize to sum to 1.0
    if abs(weight_sum - 1.0) > 0.01:  # Allow small floating point errors
        logger.info(f"{context}: Weights sum to {weight_sum:.3f}, normalizing to 1.0")
        normalized = {k: v / weight_sum for k, v in objective_weights.items()}
        
        # Verify normalization
        new_sum = sum(normalized.values())
        if abs(new_sum - 1.0) > 0.01:
            logger.warning(f"{context}: Normalization failed (sum={new_sum:.3f}), using defaults")
            return default_weights.copy() if default_weights else None
        
        return normalized
    
    return objective_weights.copy()


def get_effective_milp_objective_weights(
    goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]],
) -> Tuple[Optional[Dict[str, float]], Dict[str, float]]:
    """
    Weights from the goal (raw, may be partial / unnormalized) and MILP-ready dict
    (validated + normalized to sum 1.0, with defaults for missing keys).
    """
    raw: Optional[Dict[str, float]] = None
    if goal is not None:
        ow = None
        if isinstance(goal, DynamicScenarioConfig):
            ow = goal.objective_weights
        elif hasattr(goal, "objective_weights"):
            ow = getattr(goal, "objective_weights", None)
        if ow:
            raw = dict(ow)
    # Default when goal omits weights: aligns with Manual Optimization balanced sliders
    # (explicit capacity slot; redesign/relocation MILP ignore "capacity" as a weighted
    # objective term — see MILP subgraph weight mapping.)
    default_weights = {
        "utilization": 0.5,
        "accessibility": 0.5,
        "cost": 0.0,
        "capacity": 0.0,
    }
    effective = validate_and_normalize_weights(
        raw,
        default_weights=default_weights,
        context="MILP",
    )
    if not effective:
        effective = default_weights.copy()
    return raw, effective


def _expansion_levers_for_goal(goal: Any) -> Dict[str, bool]:
    from dno_core.models.goals import default_expansion_levers

    levers = getattr(goal, "expansion_levers", None)
    if isinstance(levers, dict):
        return {
            "test_integration": bool(levers.get("test_integration", False)),
            "shift_increase": bool(levers.get("shift_increase", False)),
            "add_device": bool(levers.get("add_device", False)),
        }
    modes = getattr(goal, "optimization_modes", None) or []
    if hasattr(goal, "get_optimization_modes"):
        try:
            modes = goal.get_optimization_modes()
        except Exception:
            pass
    return default_expansion_levers(list(modes))


def is_distance_first_open_referral(
    goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]],
    effective_weights: Dict[str, float],
) -> bool:
    """Redesign-only Open Referral with distance-dominant objective weights."""
    if goal is None:
        return False
    wu = float(effective_weights.get("utilization") or 0)
    wa = float(effective_weights.get("accessibility") or 0)
    modes = getattr(goal, "optimization_modes", None) or []
    if hasattr(goal, "get_optimization_modes"):
        try:
            modes = goal.get_optimization_modes()
        except Exception:
            pass
    levers = _expansion_levers_for_goal(goal)
    redesign_only = "redesign" in modes and not any(
        levers.get(k) for k in ("test_integration", "shift_increase", "add_device")
    )
    return redesign_only and wu <= 0.05 and wa >= 0.95


def apply_distance_first_milp_profile(
    config: EngineConfig,
    goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]],
    effective_weights: Dict[str, float],
) -> EngineConfig:
    """Overlay Open Referral MILP guards — no solver structure change."""
    if not is_distance_first_open_referral(goal, effective_weights):
        return config
    return replace(
        config,
        max_lab_utilization=1.0,
        milp_allow_asd_regression=True,
        milp_min_distance_gain_km=0.0,
        # Keep subgraph size ≥300 when older configs store a lower cap.
        milp_max_subgraph_size=max(config.milp_max_subgraph_size, 300),
        milp_timeout_seconds=max(config.milp_timeout_seconds, 360),
    )


def _largest_admin_area_1_blob(state: NetworkState) -> Tuple[int, int, int]:
    """Count admin_area_1 partitions and max sites / F×L MILP size per blob."""
    fac_by_a1: Dict[str, int] = defaultdict(int)
    lab_by_a1: Dict[str, int] = defaultdict(int)
    for f in state.facilities.values():
        if f.admin_area_1:
            fac_by_a1[f.admin_area_1] += 1
    for lab in state.labs.values():
        if lab.admin_area_1:
            lab_by_a1[lab.admin_area_1] += 1
    a1_keys = set(fac_by_a1) | set(lab_by_a1)
    if not a1_keys:
        return 0, 0, 0
    max_sites = max(fac_by_a1.get(k, 0) + lab_by_a1.get(k, 0) for k in a1_keys)
    max_vars = max(fac_by_a1.get(k, 0) * lab_by_a1.get(k, 0) for k in a1_keys)
    return len(a1_keys), max_sites, max_vars


def resolve_milp_boundary_level(
    state: NetworkState,
    candidate: str,
    explicit_override: Optional[str] = None,
    *,
    max_subgraph_size: int = 300,
) -> Tuple[str, str]:
    """
    Refine admin_area_1 → admin_area_2 when any admin_area_1 blob exceeds the
    MILP size budget and finer admin_area_2 labels exist on the network.
    """
    if explicit_override:
        return explicit_override, "explicit_override"
    if candidate != "admin_area_1":
        return candidate, "unchanged_feasible"

    a2 = {
        x.admin_area_2
        for x in (*state.facilities.values(), *state.labs.values())
        if x.admin_area_2
    }
    if len(a2) < 2:
        return candidate, "unchanged_feasible"

    n_a1, max_sites, max_vars = _largest_admin_area_1_blob(state)
    var_budget = max(2500, (max_subgraph_size * max_subgraph_size) // 15)
    too_big = max_sites > max_subgraph_size or max_vars > var_budget
    if too_big and len(state.facilities) >= 40:
        reason = (
            "refined_oversized_blob_multi_province"
            if n_a1 > 1
            else "refined_single_district_oversized"
        )
        return "admin_area_2", reason
    if n_a1 > 1:
        return candidate, "unchanged_multi_province"
    return candidate, "unchanged_feasible"


@dataclass
class Subgraph:
    """Admin-bounded subgraph for MILP solving."""
    admin_area: str
    facilities: List[Facility]
    labs: List[Facility]
    problem_type: str  # "overutilized", "underutilized", "imbalanced"


@dataclass
class SubgraphSolution:
    """Solution for a single subgraph MILP."""
    subgraph: Subgraph
    assignments: Dict[str, str]  # facility_id → lab_id
    objective_value: float
    solved: bool
    solve_time_seconds: float


class MILPDecompositionSolver:
    """
    Heuristic-driven MILP decomposition for referral redesign.
    
    Approach:
    1. Heuristic identifies problem areas
    2. Extract small, admin-bounded subgraphs
    3. Solve each subgraph with MILP (parallel)
    4. Merge solutions
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize MILP solver.
        
        Args:
            config: Engine configuration
        """
        self.config = config
        self.timeout = config.milp_timeout_seconds
        self.max_subgraph_size = config.milp_max_subgraph_size
        # Lazily created in the running loop; bounds concurrent CBC subprocesses.
        self._cbc_semaphore: Optional["asyncio.Semaphore"] = None

    def _get_cbc_semaphore(self) -> "asyncio.Semaphore":
        """Bounded gate for parallel CBC solves, created lazily on the active loop."""
        if self._cbc_semaphore is None:
            self._cbc_semaphore = asyncio.Semaphore(_default_parallel_solves(self.config))
        return self._cbc_semaphore

    def _recost_modes_active(self) -> bool:
        allocs = getattr(self.config, "sample_transport_mode_allocations", None)
        return bool(allocs)

    def _blended_mode_cpk(self) -> float:
        allocs = getattr(self.config, "sample_transport_mode_allocations", None) or []
        if not allocs:
            return resolve_sample_transport_cpk(None, self.config)
        catalog = getattr(self.config, "sample_transport_mode_catalog", None) or {}
        total = 0.0
        for mode_id, frac in allocs:
            entry = catalog.get(str(mode_id), {}) or {}
            cpk = float(entry.get("cost_per_km") or 0)
            if cpk <= 0:
                cpk = resolve_sample_transport_cpk(None, self.config)
            total += float(frac) * cpk
        return total if total > 0 else resolve_sample_transport_cpk(None, self.config)

    def _cost_per_km_for_od(
        self,
        state: NetworkState,
        origin_facility_id: str,
        dest_facility_id: str,
        fallback_cpk: float,
    ) -> float:
        """Route-level cost/km when a matching active transport route exists; else Modes catalog."""
        if self._recost_modes_active():
            return self._blended_mode_cpk()
        matched = None
        for r in getattr(state, "transport_routes", None) or []:
            if not getattr(r, "is_active", True):
                continue
            if (
                r.origin_facility_id == origin_facility_id
                and r.dest_facility_id == dest_facility_id
            ):
                matched = r
                break
        return resolve_sample_transport_cpk(matched, self.config)
    
    async def solve_redesign(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, 'DynamicScenarioConfig']] = None,
        assignment_reference_state: Optional[NetworkState] = None,
        boundary_level_override: Optional[str] = None,
    ) -> List[RedesignAction]:
        """
        Execute MILP decomposition workflow.
        
        Args:
            state: Current network state (e.g. admin-area subgraph)
            goal: Optimization goal or DynamicScenarioConfig with optional objective_weights
            assignment_reference_state: Optional full network state whose referrals define
                "current" lab assignments for merge/diff (fixes cross-boundary referral drops
                in admin-area filtered runs). When None, uses ``state.referrals``.
            boundary_level_override: When set (e.g. ``"admin_area_1"``), partition MILP
                subgraphs at this level instead of ``config.admin_boundary_level``.
                Admin-area-wise runs should pass ``"admin_area_1"`` so all district labs
                appear in one subgraph.
            
        Returns:
            List of RedesignAction for referral changes
        """
        if not self.config.milp_enabled:
            logger.info("MILP disabled, skipping redesign")
            return []

        from dno_core.domain.transport_referral_utils import (
            drop_outbound_referrals_for_lab_origins,
        )

        state = drop_outbound_referrals_for_lab_origins(state)
        
        self._milp_boundary_override = boundary_level_override
        if boundary_level_override:
            logger.info(
                f"MILP subgraph boundary override: {boundary_level_override} "
                f"(config default: {self.config.admin_boundary_level})"
            )
        
        try:
            return await self._solve_redesign_core(state, goal, assignment_reference_state)
        finally:
            self._milp_boundary_override = None

    def _effective_boundary_level(self) -> str:
        """Subgraph / merge boundary: override (admin-area-wise MILP) or config."""
        ov = getattr(self, "_milp_boundary_override", None)
        if ov:
            return ov
        return self.config.admin_boundary_level

    async def _solve_redesign_core(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, 'DynamicScenarioConfig']] = None,
        assignment_reference_state: Optional[NetworkState] = None,
    ) -> List[RedesignAction]:
        """Inner MILP workflow (used by ``solve_redesign`` after boundary override is set)."""
        raw_weights, objective_weights = get_effective_milp_objective_weights(goal)
        saved_config = self.config
        saved_timeout, saved_max = self.timeout, self.max_subgraph_size
        self.config = apply_distance_first_milp_profile(
            self.config, goal, objective_weights
        )
        if self.config is not saved_config:
            self.timeout = self.config.milp_timeout_seconds
            self.max_subgraph_size = self.config.milp_max_subgraph_size
            logger.info(
                "Open Referral MILP profile active: max_lab_utilization=1.0, "
                "milp_allow_asd_regression=True, milp_min_distance_gain_km=0.0, "
                "max_subgraph_size=%s, timeout_seconds=%s",
                self.max_subgraph_size,
                self.timeout,
            )
        try:
            return await self._solve_redesign_core_inner(
                state, goal, assignment_reference_state, raw_weights, objective_weights
            )
        finally:
            self.config = saved_config
            self.timeout, self.max_subgraph_size = saved_timeout, saved_max

    async def _solve_redesign_core_inner(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]] = None,
        assignment_reference_state: Optional[NetworkState] = None,
        raw_weights: Optional[Dict[str, float]] = None,
        objective_weights: Optional[Dict[str, float]] = None,
    ) -> List[RedesignAction]:
        if objective_weights is None:
            raw_weights, objective_weights = get_effective_milp_objective_weights(goal)
        logger.info(
            "MILP objective weights — effective: "
            f"utilization={objective_weights.get('utilization', 0):.1%}, "
            f"accessibility(distance)={objective_weights.get('accessibility', 0):.1%}, "
            f"cost={objective_weights.get('cost', 0):.1%}"
            + (f" | raw request: {raw_weights}" if raw_weights else " | raw: (none, defaults)")
        )
        optimization_logger.log(
            state.project_id,
            "OBJECTIVE_WEIGHTS",
            "info",
            {
                "context": "milp_redesign",
                "raw_objective_weights": raw_weights,
                "effective_objective_weights": objective_weights,
                "utilization_fraction": round(objective_weights.get("utilization", 0), 6),
                "accessibility_fraction": round(objective_weights.get("accessibility", 0), 6),
                "cost_fraction": round(objective_weights.get("cost", 0), 6),
                "utilization_percent": round(objective_weights.get("utilization", 0) * 100, 2),
                "accessibility_percent": round(objective_weights.get("accessibility", 0) * 100, 2),
                "cost_percent": round(objective_weights.get("cost", 0) * 100, 2),
            },
        )
        
        # Log MILP solve_redesign call
        logger.debug(f"MILP solve_redesign called: has_goal={goal is not None}, goal_type={type(goal).__name__ if goal else None}, num_referrals={len(state.referrals)}")
        
        # 1. Identify problem areas (pass goal to use goal-specific thresholds)
        problem_labs = self._identify_problem_labs(state, goal)
        
        logger.info(f"MILP redesign: Identified {len(problem_labs)} candidate labs")
        if problem_labs:
            overutilized = [p for p in problem_labs if p["type"] == "overutilized"]
            underutilized = [p for p in problem_labs if p["type"] == "underutilized"]
            asd_optimization = [p for p in problem_labs if p["type"] == "asd_optimization"]
            
            if asd_optimization:
                logger.info(f"  ASD optimization mode: {len(asd_optimization)} labs included")
            else:
                logger.info(f"  Utilization mode: {len(overutilized)} overutilized, {len(underutilized)} underutilized")
                if overutilized:
                    sample_over = [(p['lab_id'][:8], f"{p['utilization']:.1%}") for p in overutilized[:3]]
                    logger.info(f"  Sample overutilized labs: {sample_over}")
                if underutilized:
                    sample_under = [(p['lab_id'][:8], f"{p['utilization']:.1%}") for p in underutilized[:3]]
                    logger.info(f"  Sample underutilized labs: {sample_under}")
        
        if not problem_labs:
            logger.info("No problem areas identified - no redesign needed")
            return []
        
        # 2. Extract subgraphs (inject neighbour labs only on global runs, not admin-area slices)
        reference_state = assignment_reference_state or state
        partition_mode = reference_state is not state
        # Cross-district injection only when project config allows cross-boundary referrals.
        boundaries_enforced = (
            getattr(self.config, "admin_boundary_enabled", False)
            and not getattr(self.config, "allow_cross_boundary", False)
        )
        inject_neighbor_labs = not partition_mode and not boundaries_enforced
        subgraphs = self._extract_subgraphs(
            problem_labs,
            state,
            inject_neighbor_labs=inject_neighbor_labs,
        )
        logger.info(f"MILP redesign: Extracted {len(subgraphs)} subgraphs")
        if subgraphs:
            logger.info(f"  Subgraph admin areas: {[sg.admin_area for sg in subgraphs]}")
            logger.info(f"  Subgraph sizes: {[(sg.admin_area, len(sg.facilities), len(sg.labs)) for sg in subgraphs[:3]]}")
        
        if not subgraphs:
            logger.warning("No subgraphs extracted - cannot solve redesign")
            return []
        
        # 3. Solve each subgraph in parallel
        import asyncio
        cbc_threads = _cbc_threads_per_solve(self.config, len(subgraphs))
        logger.info(
            f"Solving {len(subgraphs)} subgraphs in parallel (cbc_threads={cbc_threads})..."
        )

        # Create tasks for parallel execution
        tasks = [
            self._solve_subgraph(subgraph, state, objective_weights, cbc_threads=cbc_threads, goal=goal)
            for subgraph in subgraphs
        ]
        
        # Execute all subgraph solves in parallel
        solutions = await asyncio.gather(*tasks)
        
        # Filter to only include solved solutions
        solutions = [sol for sol in solutions if sol.solved]
        
        # 4. Merge solutions
        ref = assignment_reference_state or state
        actions = self._merge_solutions(solutions, state, ref)
        
        logger.info(f"MILP redesign complete: {len(solutions)}/{len(subgraphs)} subgraphs solved, {len(actions)} redesign actions generated")
        if actions:
            logger.info(f"  Sample redesigns: {[(a.facility_id[:8], a.old_lab_id[:8], '→', a.new_lab_id[:8], f'{a.distance_change_km:.1f}km') for a in actions[:5]]}")
        else:
            logger.warning("  No redesign actions generated despite problem labs and subgraphs")
        
        return actions
    
    def _identify_problem_labs(
        self,
        state: NetworkState,
        goal: Optional[Union[OptimizationGoal, DynamicScenarioConfig]] = None
    ) -> List[Dict]:
        """
        Fast heuristic to identify problem areas.
        
        Uses goal-specific thresholds if available (e.g., UtilizationBalanceGoal),
        otherwise falls back to config thresholds.
        
        When distance weight is high (>0.6), includes ALL labs for ASD optimization,
        not just those with utilization problems.
        
        Args:
            state: Network state
            goal: Optional goal with utilization targets
        
        Returns list of problem descriptions.
        """
        problems = []
        
        # User requirement: "Main is always reducing ASD for the admin area"
        # ALWAYS include all admin areas for optimization (not just utilization problems)
        # The objective weights control the trade-off, but we should always try to improve
        optimize_all_for_asd = True
        logger.info(f"Including ALL admin areas for optimization (user requirement: always reduce ASD)")
        
        # Get utilization thresholds from goal if available
        threshold_high = float(self.config.utilization_threshold_high)
        threshold_low = float(self.config.utilization_threshold_low)
        
        if goal:
            # Check if goal has utilization targets (UtilizationBalanceGoal)
            if hasattr(goal, 'max_utilization_target'):
                threshold_high = float(goal.max_utilization_target)
                logger.info(f"Using goal-specific max utilization threshold: {threshold_high:.1%}")
            if hasattr(goal, 'min_utilization_target'):
                threshold_low = float(goal.min_utilization_target)
                logger.info(f"Using goal-specific min utilization threshold: {threshold_low:.1%}")
            # Check DynamicScenarioConfig for utilization_targets
            elif hasattr(goal, 'utilization_targets') and goal.utilization_targets:
                threshold_high = float(goal.utilization_targets.get('max', threshold_high))
                threshold_low = float(goal.utilization_targets.get('min', threshold_low))
                logger.info(f"Using DynamicScenarioConfig utilization thresholds: min={threshold_low:.1%}, max={threshold_high:.1%}")
        
        # Calculate lab utilizations
        lab_utils = self._calculate_utilizations(state)
        
        # If optimizing all for ASD, include every lab as a "candidate" for optimization
        if optimize_all_for_asd:
            for lab_id, util in lab_utils.items():
                problems.append({
                    "lab_id": lab_id,
                    "type": "asd_optimization",  # New type for distance-first optimization
                    "utilization": util,
                })
            logger.info(f"ASD-first mode: Including all {len(problems)} labs for distance optimization")
        else:
            # Standard utilization-based problem identification
            for lab_id, util in lab_utils.items():
                if util > threshold_high:
                    problems.append({
                        "lab_id": lab_id,
                        "type": "overutilized",
                        "utilization": util,
                    })
                elif util < threshold_low:
                    problems.append({
                        "lab_id": lab_id,
                        "type": "underutilized",
                        "utilization": util,
                    })
            
            logger.info(f"Problem labs identified: {len([p for p in problems if p['type'] == 'overutilized'])} overutilized, {len([p for p in problems if p['type'] == 'underutilized'])} underutilized (thresholds: {threshold_low:.1%}-{threshold_high:.1%})")
        
        return problems
    
    def _calculate_utilizations(self, state: NetworkState) -> Dict[str, float]:
        """Calculate utilization per lab."""
        capacities = {}
        demands = {}
        
        # Calculate capacities from devices
        for device in state.devices.values():
            lab_id = device.facility_id
            cap = device.annual_capacity(self.config.working_days_per_year)
            capacities[lab_id] = capacities.get(lab_id, 0) + cap
        
        # Calculate demands: base demand + referral demand
        for lab_id in state.labs.keys():
            # Base demand at the lab itself
            base_demand = state.test_volumes.get(lab_id, 0)
            demands[lab_id] = base_demand
        
        # Add referral demand
        for ref in state.referrals:
            lab_id = ref.lab_id
            vol = state.test_volumes.get(ref.facility_id, 0)
            demands[lab_id] = demands.get(lab_id, 0) + vol
        
        # Calculate utilizations.
        # Seed every known lab with 0.0 so that labs with no devices yet (newly
        # converted facilities) are still included in _identify_problem_labs and
        # get a redesign subgraph.  Without this, new labs never appear in the
        # `optimize_all_for_asd` pass and facilities that should route to them
        # (like NAMROOP TE → Naharkatia CHC) are never redesigned in the real
        # solve_redesign pass — only in discarded hypothesis evaluations.
        utils = {lab_id: 0.0 for lab_id in state.labs}
        for lab_id, cap in capacities.items():
            demand = demands.get(lab_id, 0)
            utils[lab_id] = demand / cap if cap > 0 else 0
        
        logger.debug(f"Calculated utilizations for {len(utils)} labs: {[f'{lid[:8]}:{u:.1%}' for lid, u in list(utils.items())[:5]]}")
        return utils
    
    def _extract_subgraphs(
        self,
        problem_labs: List[Dict],
        state: NetworkState,
        inject_neighbor_labs: bool = False,
    ) -> List[Subgraph]:
        """
        Extract admin-bounded subgraphs around problem areas.
        """
        subgraphs = []
        processed_areas: Set[str] = set()
        # Primary lab per HF for savings baseline (last referral wins if multiples).
        current_assignments = {
            ref.facility_id: ref.lab_id for ref in state.referrals
        }

        for problem in problem_labs:
            lab_id = problem["lab_id"]
            lab = state.labs.get(lab_id)
            if not lab:
                continue
            
            # Get admin area (respect MILP boundary override for subgraph partitioning)
            level = self._effective_boundary_level()
            if level == "admin_area_1":
                admin_area = lab.admin_area_1
            else:
                admin_area = lab.admin_area_2
            
            if not admin_area or admin_area in processed_areas:
                continue
            
            processed_areas.add(admin_area)
            
            # Get all facilities and labs in this admin area
            facilities_in_area = [
                f for f in state.facilities.values()
                if self._get_admin_area(f) == admin_area
            ]
            
            labs_in_area = [
                l for l in state.labs.values()
                if self._get_admin_area(l) == admin_area
            ]
            
            # Check size limit
            if len(facilities_in_area) + len(labs_in_area) > self.max_subgraph_size:
                logger.warning(f"Admin area {admin_area} exceeds max size, truncating")
                facilities_in_area = facilities_in_area[:self.max_subgraph_size // 2]
                labs_in_area = labs_in_area[:self.max_subgraph_size // 2]

            # Nearby cross-admin labs should cover high-demand savings, not just the first globally closest pair.
            if inject_neighbor_labs and facilities_in_area:
                from dno_core.domain.transport_referral_utils import is_lab_referrable

                seen = {lab.id for lab in labs_in_area}
                max_total_sites = self.max_subgraph_size
                lab_slots = max(
                    0, max_total_sites - len(facilities_in_area) - len(labs_in_area)
                )
                min_gain_km = float(
                    getattr(self.config, "milp_neighbor_labs_min_gain_km", 2.0) or 0.0
                )
                per_facility_k = int(
                    getattr(self.config, "milp_neighbor_labs_per_facility_k", 3) or 0
                )
                k_config = int(getattr(self.config, "milp_neighbor_labs_k", 10) or 0)
                if k_config <= 0:
                    inject_limit = min(
                        lab_slots, max(10, len(facilities_in_area) // 10)
                    )
                else:
                    inject_limit = min(
                        lab_slots,
                        max(k_config, len(facilities_in_area) // 15),
                    )
                injected_count = 0

                if inject_limit > 0 and per_facility_k > 0:
                    per_lab_score: Dict[str, float] = defaultdict(float)
                    per_lab_helped_hfs: Dict[str, int] = defaultdict(int)
                    per_lab_best_distance: Dict[str, float] = {}
                    lab_by_id: Dict[str, Facility] = {}

                    for facility in facilities_in_area:
                        volume = float(state.test_volumes.get(facility.id, 0) or 0)
                        if volume <= 0 or not facility.has_coordinates:
                            continue

                        current_lab = state.labs.get(
                            current_assignments.get(facility.id)
                        )
                        if current_lab and current_lab.has_coordinates:
                            current_distance = DistanceCalculator.facility_distance(
                                facility, current_lab
                            )
                        else:
                            # No referral yet: baseline is nearest in-area lab.
                            current_distance = min(
                                (
                                    d
                                    for d in (
                                        DistanceCalculator.facility_distance(
                                            facility, lab
                                        )
                                        for lab in labs_in_area
                                        if lab.has_coordinates
                                    )
                                    if d is not None
                                ),
                                default=None,
                            )
                        if current_distance is None:
                            continue

                        local_candidates = []
                        for lab in state.labs.values():
                            if lab.id in seen or self._get_admin_area(lab) == admin_area:
                                continue
                            if not lab.has_coordinates or not is_lab_referrable(
                                state, lab.id
                            ):
                                continue
                            distance = DistanceCalculator.facility_distance(
                                facility, lab
                            )
                            if distance is None:
                                continue
                            saving = current_distance - distance
                            if saving <= min_gain_km:
                                continue
                            local_candidates.append((distance, lab.id, lab, saving))

                        local_candidates.sort(key=lambda row: (row[0], row[1]))
                        for distance, lab_id, lab, saving in local_candidates[
                            :per_facility_k
                        ]:
                            per_lab_score[lab_id] += saving * volume
                            per_lab_helped_hfs[lab_id] += 1
                            per_lab_best_distance[lab_id] = min(
                                per_lab_best_distance.get(lab_id, float("inf")),
                                distance,
                            )
                            lab_by_id[lab_id] = lab

                    ranked = sorted(
                        per_lab_score.keys(),
                        key=lambda lid: (
                            -per_lab_score[lid],
                            -per_lab_helped_hfs[lid],
                            per_lab_best_distance[lid],
                            lid,
                        ),
                    )
                    for lab_id in ranked:
                        if injected_count >= inject_limit:
                            break
                        if len(labs_in_area) + len(facilities_in_area) >= max_total_sites:
                            break
                        if lab_id in seen:
                            continue
                        labs_in_area.append(lab_by_id[lab_id])
                        seen.add(lab_id)
                        injected_count += 1

                    if injected_count:
                        logger.info(
                            "Subgraph %s: injected %d demand-weighted out-of-area lab candidate(s)",
                            admin_area,
                            injected_count,
                        )
            
            subgraphs.append(Subgraph(
                admin_area=admin_area,
                facilities=facilities_in_area,
                labs=labs_in_area,
                problem_type=problem["type"],
            ))
        
        return subgraphs
    
    def _get_admin_area(self, facility: Facility) -> Optional[str]:
        """Get admin area based on configured (or overridden) boundary level."""
        if self._effective_boundary_level() == "admin_area_1":
            return facility.admin_area_1
        return facility.admin_area_2
    
    async def _solve_subgraph(
        self,
        subgraph: Subgraph,
        state: NetworkState,
        objective_weights: Optional[Dict[str, float]] = None,
        cbc_threads: Optional[int] = None,
        goal: Optional[Any] = None,
    ) -> SubgraphSolution:
        """
        Solve MILP for single subgraph.
        
        Variables:
        - x[i,j] ∈ {0,1}: facility i assigned to lab j
        
        Objective:
        Minimize: Σ(cost_ij * x[i,j])
        
        Where cost_ij depends on objective_weights (default: distance).
        """
        import time
        start_time = time.time()
        
        try:
            import pulp
        except ImportError:
            logger.warning("PuLP not installed, using greedy fallback")
            return self._solve_greedy_fallback(subgraph, state, start_time)
        
        # Setup problem
        prob = pulp.LpProblem(f"Subgraph_{subgraph.admin_area}", pulp.LpMinimize)
        
        facilities = subgraph.facilities
        labs = subgraph.labs
        
        if not labs:
            return SubgraphSolution(
                subgraph=subgraph,
                assignments={},
                objective_value=0,
                solved=False,
                solve_time_seconds=time.time() - start_time,
            )

        
        # Decision variables
        from dno_core.domain.transport_referral_utils import is_lab_referrable

        x = {}
        for f in facilities:
            for l in labs:
                x[(f.id, l.id)] = pulp.LpVariable(f"x_{f.id}_{l.id}", cat="Binary")

        # Dead labs (removed / zero operational capacity) cannot receive assignments
        dead_lab_pairs = 0
        for f in facilities:
            for l in labs:
                if not is_lab_referrable(state, l.id):
                    prob += x[(f.id, l.id)] == 0
                    dead_lab_pairs += 1
        if dead_lab_pairs:
            logger.info(
                "MILP: forbade %d facility×lab pairs targeting non-referrable labs",
                dead_lab_pairs,
            )
        
        # Redesign MILP objective weights.
        #
        # Objective = w_dist * distance_cost  +  w_util * utilization_penalty
        #
        # w_util is kept small when util-weight < 0.3 (distance-focused) so the
        # solver chases ASD aggressively.  It grows as util-weight rises so the
        # solver actively balances load.  The hard capacity cap (base_max_util_cap)
        # is a separate, complementary guard.
        #
        # Weight mapping:
        #   dist-focused  (wu ≤ 0.3): w_dist≈1.0, w_util≈0.05  — distance dominates
        #   balanced      (wu ≈ 0.5): w_dist≈0.7, w_util≈0.30  — default
        #   util-focused  (wu ≥ 0.6): w_dist≈0.4, w_util≈0.60  — util dominates
        w_util = 0.0
        w_dist = 1.0
        w_cost = 0.0
        _user_wu = 0.5  # fallback for cap computation
        _dist_first_profile = is_distance_first_open_referral(
            goal, objective_weights or {}
        )

        if objective_weights:
            _raw_cost = objective_weights.get("cost", 0.0)
            _user_dist = objective_weights.get("accessibility", 1.0)
            _user_wu = float(objective_weights.get("utilization", 0.5))
            # Scale w_dist so it reflects how much the user wants distance vs util.
            # Normalise against the non-util portion so cost still works.
            total_non_util = _user_dist + _raw_cost
            if total_non_util > 0:
                w_dist = _user_dist / total_non_util
                w_cost = _raw_cost / total_non_util
            # Blend w_util into objective based on mode:
            #   dist-focused  (wu ≤ 0.3): tiny util influence — distance dominates
            #   balanced      (wu ≈ 0.5): moderate util — balance between dist and util
            #   util-focused  (wu ≥ 0.6): strong util — load balancing is primary goal
            if _user_wu <= 0.3:
                w_util = 0.05   # near-zero, just a tiebreaker
            elif _user_wu >= 0.6:
                w_util = 0.60   # util strongly influences routing
            else:
                # linear interpolation between 0.05 and 0.60 in the 0.3–0.6 range
                w_util = 0.05 + (_user_wu - 0.3) / 0.3 * 0.55
            # Re-normalise so dist+util+cost sum to 1 for consistent objective scale.
            _total = w_dist + w_util + w_cost
            w_dist /= _total
            w_util /= _total
            if w_cost > 0:
                w_cost /= _total
            logger.info(
                f"MILP subgraph {subgraph.admin_area}: w_dist={w_dist:.3f}, w_util={w_util:.3f}, w_cost={w_cost:.3f} "
                f"(user wu={_user_wu:.2f}, weights={objective_weights})"
            )
        else:
            logger.debug(
                f"MILP subgraph {subgraph.admin_area}: default pure-distance weights "
                f"w_dist={w_dist}, w_util={w_util}, w_cost={w_cost}"
            )
        
        # Current lab capacities and demands (for projected utilization in the objective)
        lab_capacities = {}
        lab_demands = {}
        
        for l in labs:
            lab_capacity = state.lab_capacity(l.id, self.config.working_days_per_year)
            lab_capacities[l.id] = lab_capacity
            
            # Current demand (base + referrals)
            base_demand = state.test_volumes.get(l.id, 0)
            referral_demand = sum(
                state.test_volumes.get(ref.facility_id, 0)
                for ref in state.referrals
                if ref.lab_id == l.id
            )
            lab_demands[l.id] = base_demand + referral_demand
        
        # Build current assignment map for action penalty
        current_assignments = {}
        for ref in state.referrals:
            current_assignments[ref.facility_id] = ref.lab_id
        
        # Subgraph facility×lab distances (single source for objective + ASD cap)
        d_mat: Dict[Tuple[str, str], float] = {}
        for f in facilities:
            for l in labs:
                d_raw = DistanceCalculator.facility_distance(f, l)
                d_mat[(f.id, l.id)] = float(d_raw) if d_raw is not None else 100.0
        
        # Demand-weighted travel baseline for this subgraph (for ASD preservation)
        # Uses current lab from full network; unassigned referrers use best in-subgraph distance
        baseline_weighted = 0.0
        total_subgraph_demand = 0.0
        for f in facilities:
            vol = float(state.test_volumes.get(f.id, 0) or 0)
            if vol <= 0:
                continue
            total_subgraph_demand += vol
            cur = current_assignments.get(f.id)
            lab_ref = state.labs.get(cur) if cur else None
            if lab_ref is not None:
                d0 = DistanceCalculator.facility_distance(f, lab_ref)
                db = float(d0) if d0 is not None else 0.0
            else:
                db = min((d_mat[(f.id, l.id)] for l in labs), default=0.0)
            baseline_weighted += vol * db
        
        baseline_asd_sub = (
            baseline_weighted / total_subgraph_demand if total_subgraph_demand > 0 else 0.0
        )
        logger.info(
            f"Subgraph baseline demand-weighted distance: {baseline_weighted:.2f} km·tests "
            f"(ASD {baseline_asd_sub:.2f} km, demand={total_subgraph_demand:.0f})"
        )

        # CoverageGoal: forbid facility→lab pairs beyond max road-equivalent distance
        coverage_max_km: Optional[float] = None
        if goal is not None:
            from dno_core.models.goals import CoverageGoal

            if isinstance(goal, CoverageGoal):
                coverage_max_km = float(goal.max_distance_km)
        if coverage_max_km is not None:
            _road_factor = 1.4
            forbidden = 0
            for f in facilities:
                for l in labs:
                    if d_mat[(f.id, l.id)] * _road_factor > coverage_max_km + 1e-6:
                        prob += x[(f.id, l.id)] == 0
                        forbidden += 1
            if forbidden:
                logger.info(
                    "CoverageGoal MILP: forbade %s facility×lab pairs beyond %.1f km road-equiv",
                    forbidden,
                    coverage_max_km,
                )
        
        # Defined early so both the co-location guard and the capacity constraint can use it.
        # Upper utilization cap — the CEILING a lab can be filled to by new referrals.
        # base_demand subtraction is the primary guard against piling on overloaded labs;
        # this cap governs healthy labs and sets the acceptable ceiling per mode:
        #
        #   util-focused  (wu ≥ 0.6) → 0.95  no overutil allowed on healthy labs;
        #                                       w_util in objective drives load UP from idle labs
        #   balanced      (wu 0.3–0.6) → 0.95  default
        #   dist-focused  (wu ≤ 0.3) → 1.15  allow notable overutil to chase ASD gains;
        #                                       already-overloaded labs are still frozen by the
        #                                       base_demand / overutil_relief guard
        if _user_wu <= 0.3:
            base_max_util_cap = 1.15  # dist-focused: accept overutil for ASD gains
        else:
            base_max_util_cap = float(getattr(self.config, 'max_lab_utilization', 0.95))
        # Pre-MILP per-lab utilization — used in debug logging and frozen-lab detection
        _pre_redesign_utils = self._calculate_utilizations(state)
        logger.info(
            f"MILP redesign cap: base_max_util_cap={base_max_util_cap:.0%} "
            f"(wu={_user_wu:.2f}, overloaded labs will be frozen regardless)"
        )

        # Constraint: Don't change a lab's own facility referral
        # If a facility is also a lab, it should refer to itself
        for f in facilities:
            if f.is_lab and f.id in [l.id for l in labs]:
                # This facility is also a lab - it must refer to itself
                prob += x[(f.id, f.id)] == 1
                logger.debug(f"Locking lab {f.id[:8]} to refer to itself")
        
        # Constraint: Don't reassign facilities that are co-located with their lab (distance = 0)
        # These are typically the lab's own campus facilities.
        # When a co-located facility's volume exceeds the lab's 0.95-cap referral budget,
        # we still lock it (it has nowhere else to go) but we raise the capacity cap for
        # that specific lab to 1.0 so the MILP remains feasible.  Tracking which labs need
        # the cap raised is done in a set that the capacity-constraint loop checks below.
        colocated_locked = 0
        labs_with_cap_override: set = set()  # lab ids where cap must be raised to 1.0
        for f in facilities:
            current_lab_id = current_assignments.get(f.id)
            if current_lab_id:
                current_lab = [l for l in labs if l.id == current_lab_id]
                if current_lab:
                    dist = DistanceCalculator.facility_distance(f, current_lab[0])
                    if dist is not None and dist < 0.1:  # Co-located (< 100m)
                        # Always lock co-located facility — it physically cannot go elsewhere.
                        prob += x[(f.id, current_lab_id)] == 1
                        colocated_locked += 1
                        # If locking would blow the capacity budget, raise cap to 1.0
                        lab_obj = current_lab[0]
                        lab_raw_cap = state.lab_capacity(lab_obj.id, self.config.working_days_per_year)
                        lab_own_vol = float(state.test_volumes.get(lab_obj.id, 0))
                        lab_referral_cap = max(0.0, lab_raw_cap * base_max_util_cap - lab_own_vol)
                        fac_vol = float(state.test_volumes.get(f.id, 0))
                        if fac_vol > lab_referral_cap:
                            labs_with_cap_override.add(lab_obj.id)
                            logger.info(
                                f"⚡ Co-located facility {f.name} (vol={fac_vol:.0f}) exceeds "
                                f"lab {lab_obj.name} referral budget ({lab_referral_cap:.0f}) — "
                                "raising lab cap to 1.0 to maintain feasibility"
                            )
        
        if colocated_locked > 0:
            logger.info(f"Locked {colocated_locked} co-located facilities (distance < 0.1km) to their current labs")
        
        # Calculate dynamic action penalty based on subgraph distances
        # Research shows penalties should be 5-10% of average objective values
        all_distances = []
        for f in facilities:
            for l in labs:
                dist = DistanceCalculator.facility_distance(f, l)
                if dist is not None:
                    all_distances.append(dist)
        
        # Action penalty: disabled to allow aggressive redesign like OPTIDX PDF
        # OPTIDX achieved 25-61% ASD reductions, suggesting they had no action penalty
        # The objective function's distance and utilization components will drive optimization
        action_penalty = 0.0
        logger.info(f"Action penalty disabled to allow aggressive redesign (OPTIDX strategy)")
        
        # Subgraph ASD: facilities beyond this get extra distance weight (OPTIDX: reduce % beyond ASD)
        avg_distance = sum(all_distances) / len(all_distances) if all_distances else 25.0
        subgraph_asd = avg_distance
        
        fallback_cpk = float(self.config.cost_per_km)

        # Projected utilization penalty — computed per (facility, lab) pair in the cost loop.
        # For each facility assigned to lab l, we estimate how loaded l would become and
        # apply a mode-dependent penalty/reward.  See inline comments below.
        lab_util_vals = _pre_redesign_utils  # already computed above

        costs = {}
        for f in facilities:
            facility_demand = state.test_volumes.get(f.id, 0)
            facility_population = f.population or 1  # Use population for weighting, default to 1
            current_lab = current_assignments.get(f.id)
            # OPTIDX-style: extra distance weight for facilities currently beyond subgraph mean pairwise distance
            current_dist = 0.0
            if current_lab:
                current_lab_obj = next((l for l in labs if l.id == current_lab), None)
                if current_lab_obj:
                    current_dist = d_mat[(f.id, current_lab)]
                else:
                    lo = state.labs.get(current_lab)
                    if lo:
                        dd = DistanceCalculator.facility_distance(f, lo)
                        current_dist = float(dd) if dd is not None else 0.0
            beyond_asd_boost = (
                1.0
                if _dist_first_profile
                else (1.5 if current_dist > subgraph_asd else 1.0)
            )
            
            for l in labs:
                dist = d_mat[(f.id, l.id)]

                # Distance component: 0 km → 0, 200 km → 100 (linear, clipped)
                distance_penalty = min(dist / 2.0, 100.0)

                # Utilization component — behaviour is mode-dependent:
                #
                # util-focused (wu ≥ 0.6): REWARD assigning to underutilised labs (negative
                #   penalty → lower cost), penalise overutil.  This actively balances load
                #   and pushes utilization up from idle labs, even at slight distance cost.
                # balanced (wu 0.3–0.6): moderate penalty for heavily loaded labs only.
                # dist-focused (wu ≤ 0.3): near-zero util influence; distance dominates.
                lab_cap = lab_capacities.get(l.id, 1) or 1
                marginal_util_contrib = float(facility_demand) / float(lab_cap)
                current_lab_util = lab_util_vals.get(l.id, 0.0)
                projected_util = current_lab_util + marginal_util_contrib

                if _user_wu >= 0.6:
                    # util-focused: reward filling underutilised labs; penalise overutil
                    if projected_util > 1.0:
                        # Above 100%: steep penalty — avoid this
                        util_penalty = 100.0 + (projected_util - 1.0) * 800.0
                    elif projected_util > 0.90:
                        # 90-100%: slight penalty (fine but not ideal)
                        util_penalty = (projected_util - 0.90) / 0.10 * 20.0 + 10.0
                    else:
                        # Below 90%: reward for filling — lower cost for heavier labs
                        # (up to the sweet spot).  Scales from -30 at 90% to 0 at 0%.
                        util_penalty = -1 * (projected_util / 0.90) * 30.0
                elif _user_wu <= 0.3:
                    # dist-focused: minimal util penalty — only penalise extreme overutil
                    if projected_util > 1.15:
                        util_penalty = (projected_util - 1.15) * 200.0
                    else:
                        util_penalty = 0.0
                else:
                    # balanced: moderate penalty above 85%
                    if projected_util > 0.85:
                        util_penalty = min((projected_util - 0.85) * 300.0 + projected_util * 50.0, 150.0)
                    else:
                        util_penalty = 0.0

                # Financial component (only non-zero when w_cost > 0)
                cpk = self._cost_per_km_for_od(state, f.id, l.id, fallback_cpk)
                fin_cost = dist * cpk
                norm_cost = min(fin_cost / 2000.0, 1.0) * 100

                # Weighted combination
                weighted_cost = (
                    w_dist * distance_penalty * beyond_asd_boost
                    + w_util * util_penalty
                    + w_cost * norm_cost
                )

                # Population weight: higher-population sites drive the objective more
                if _dist_first_profile:
                    population_weight = 1.0
                else:
                    population_weight = facility_population / 1000.0
                    population_weight = max(0.1, min(10.0, population_weight))
                weighted_cost = weighted_cost * population_weight

                # Stickiness: tiny penalty for changing assignment (action_penalty=0 currently)
                if current_lab and current_lab != l.id:
                    weighted_cost += action_penalty

                costs[(f.id, l.id)] = weighted_cost
        
        obj_main = pulp.lpSum(
            costs[(f.id, l.id)] * x[(f.id, l.id)] for f in facilities for l in labs
        )
        
        # Constraint: each facility assigned to exactly one lab
        for f in facilities:
            prob += pulp.lpSum(x[(f.id, l.id)] for l in labs) == 1
        
        # ── Overutilization-relief mode ──────────────────────────────────────────
        # If any lab in this subgraph is currently above 100 % utilization, we
        # freeze it (no new referrals in) and raise other labs' cap so they can
        # absorb displaced demand.  The base_demand subtraction in the normal path
        # (below) prevents healthy labs from being filled past their cap.
        subgraph_lab_utils = _pre_redesign_utils  # reuse — same state, avoid duplicate call
        overloaded_lab_ids = {
            l.id for l in labs
            if subgraph_lab_utils.get(l.id, 0.0) > 1.0
        }
        overutil_relief_active = bool(overloaded_lab_ids)
        if overutil_relief_active:
            logger.info(
                f"⚡ Overutil-relief mode active for {len(overloaded_lab_ids)} lab(s) "
                f"above 100%: {[(lid[:8], f'{subgraph_lab_utils.get(lid,0):.0%}') for lid in overloaded_lab_ids]}. "
                "Freezing overloaded labs (no new demand in), receiving labs capped at 1.0."
            )

        # Capacity constraint: assigned demand ≤ lab_capacity × effective_cap
        # Default 0.95 — prevents packing labs to 100% so load spreads across sites.
        # In overutil-relief mode:
        #   - overloaded labs (>100%) are frozen: their demand cap = their current demand,
        #     so the MILP must route ALL their referrals elsewhere (not just the overflow).
        #     This forces load off the overloaded lab rather than just capping the top.
        #   - receiving (non-overloaded) labs use cap=1.0 so they can absorb the displaced load.
        # Co-located locked facilities: their volume is subtracted from the RHS so the
        # capacity constraint governs only the routable (non-locked) facilities.
        colocated_per_lab: dict = {}  # lab_id -> set of locked facility ids
        for f in facilities:
            current_lab_id = current_assignments.get(f.id)
            if current_lab_id and current_lab_id in labs_with_cap_override:
                colocated_per_lab.setdefault(current_lab_id, set()).add(f.id)

        # Multi-test per-device y[l,d,t] path (Q.1) — single-test subgraphs stay scalar.
        from dno_core.solvers.milp_capacity_y import (
            subgraph_single_test_fast_path,
            add_capability_gates,
            precommitted_per_test_demand,
            lab_y_eligibility,
            add_lab_per_device_y_constraints,
        )

        _use_y_path = not subgraph_single_test_fast_path(state, facilities, labs)
        _y_vars: Dict = {}
        _labs_y_mode: Dict[str, str] = {}
        if _use_y_path:
            # current_assignments covers both lock types (self-referral f→f and
            # co-located f→current_lab) so gating never contradicts a forced x==1.
            _gate_count = add_capability_gates(
                prob, x, state, facilities, labs,
                current_assignments=current_assignments,
            )
            logger.info(
                "MILP multi-test subgraph: capability gates=%s (locked/no-cover exempt)",
                _gate_count,
            )

        # (base_max_util_cap already defined above the co-location lock block)
        for l in labs:
            if l.id in labs_with_cap_override:
                # A co-located facility was locked here whose volume alone exceeds the
                # 0.95-cap referral budget.  Raise to 1.0 (physical max) so that after
                # we subtract the co-located volume the remaining budget is non-negative.
                # This takes priority over the overutil_relief branch below.
                lab_util_cap = 1.0
            elif overutil_relief_active and l.id in overloaded_lab_ids:
                # This lab is already above 100%.  Freeze it: don't let the MILP route
                # any additional demand here.  The constraint becomes:
                #   in_subgraph_assigned_volume ≤ current_in_subgraph_referral_load
                # (i.e., MILP can redistribute among subgraph facilities but cannot
                # bring in net new volume to this lab.)
                #
                # Critical: we set effective_capacity = current in-subgraph referral load
                # (not total demand), because assigned_volume in the constraint only sums
                # the subgraph's facility variables. Out-of-subgraph referrals are fixed
                # and not part of the MILP decision — constraining against total demand
                # would create too-large a budget and allow new assignments to fill the gap.
                colocated_fac_ids = colocated_per_lab.get(l.id, set())
                subgraph_facility_ids_frozen = {f.id for f in facilities}
                in_subgraph_referral_load = sum(
                    float(state.test_volumes.get(ref.facility_id, 0))
                    for ref in state.referrals
                    if ref.lab_id == l.id
                    and ref.facility_id in subgraph_facility_ids_frozen
                    and ref.facility_id not in colocated_fac_ids
                )
                # Cap = current in-subgraph load: MILP can't add more, can route away.
                effective_capacity = max(0.0, in_subgraph_referral_load)
                assigned_volume = pulp.lpSum(
                    state.test_volumes.get(f.id, 0) * x[(f.id, l.id)]
                    for f in facilities
                    if f.id not in colocated_fac_ids
                )
                prob += assigned_volume <= effective_capacity
                logger.info(
                    f"Lab {l.id[:8]} ({getattr(l,'name','?')[:25]}): FROZEN (overloaded "
                    f"{subgraph_lab_utils.get(l.id,0):.0%}), "
                    f"in_subgraph_cap={in_subgraph_referral_load:.0f} "
                    f"(no new in-subgraph referrals, can route away)"
                )
                continue  # skip the shared constraint-add at the bottom of the loop
            elif overutil_relief_active and l.id not in overloaded_lab_ids:
                # Receiving labs absorb displaced demand but are still capped at base_max_util_cap
                # (0.95) to leave headroom and prevent stacking all displaced volume onto one lab.
                # Only dist-focused mode (wu <= 0.3) already uses a higher 1.15 cap — keep that.
                lab_util_cap = base_max_util_cap
            else:
                lab_util_cap = base_max_util_cap

            # Per-device shift-time constraints for eligible mixed-test labs (skip frozen path).
            if _use_y_path and l.id not in overloaded_lab_ids:
                subgraph_facility_ids_y = {f.id for f in facilities}
                colocated_fac_ids_y = colocated_per_lab.get(l.id, set())
                cap_l_y = 1.0 if l.id in labs_with_cap_override else float(lab_util_cap)
                precommitted_y = precommitted_per_test_demand(
                    state,
                    l.id,
                    facilities,
                    colocated_fac_ids_y,
                    subgraph_facility_ids_y,
                    current_assignments,
                )
                eligible_y, skip_reason = lab_y_eligibility(
                    state,
                    l.id,
                    precommitted_y,
                    cap_l_y,
                    self.config.working_days_per_year,
                )
                if eligible_y:
                    add_lab_per_device_y_constraints(
                        prob,
                        x,
                        state,
                        l,
                        facilities,
                        precommitted_y,
                        cap_l_y,
                        _y_vars,
                    )
                    _labs_y_mode[l.id] = "per_device_y"
                    logger.info(
                        "Lab %s (%s): per_device_y constraints (cap_l=%.2f, y_vars=%s)",
                        l.id[:8],
                        getattr(l, "name", "?")[:25],
                        cap_l_y,
                        len(_y_vars),
                    )
                    continue
                _labs_y_mode[l.id] = f"scalar_fallback_{skip_reason or 'unknown'}"

            lab_capacity = state.lab_capacity(l.id, self.config.working_days_per_year)
            effective_capacity = lab_capacity * lab_util_cap
            colocated_fac_ids = colocated_per_lab.get(l.id, set())
            colocated_vol = sum(
                float(state.test_volumes.get(fid, 0))
                for fid in colocated_fac_ids
            )
            # Referral load from facilities OUTSIDE this subgraph already assigned to this lab.
            # These assignments are invisible to the MILP (not decision variables), so they
            # must be pre-subtracted from the budget to prevent the MILP from overcounting
            # available space and assigning more than the lab can actually handle.
            subgraph_facility_ids = {f.id for f in facilities}
            out_of_subgraph_referral_load = sum(
                float(state.test_volumes.get(ref.facility_id, 0))
                for ref in state.referrals
                if ref.lab_id == l.id
                and ref.facility_id not in subgraph_facility_ids
                and ref.facility_id not in colocated_fac_ids
            )
            # Compute current referral load on this lab from facilities in this subgraph
            # (non-colocated facilities currently assigned here).
            current_referral_load = sum(
                float(state.test_volumes.get(f.id, 0))
                for f in facilities
                if f.id not in colocated_fac_ids
                and current_assignments.get(f.id) == l.id
            )
            # Budget for referrals from this subgraph = (capacity × cap) - colocated_vol
            # - base_demand - out_of_subgraph_referral_load.
            #
            # base_demand = test_volumes[lab_id]: the lab's own historical test count.
            # out_of_subgraph_referral_load: volume already committed to this lab from
            # facilities not in this subgraph — invisible to MILP decision variables.
            # Without this subtraction, admin-area-wise and partial-subgraph runs allow
            # the MILP to over-fill labs that are already loaded by out-of-scope facilities.
            lab_base_demand = float(state.test_volumes.get(l.id, 0))
            budget_from_cap = effective_capacity - colocated_vol - lab_base_demand - out_of_subgraph_referral_load
            if budget_from_cap < current_referral_load:
                effective_capacity = current_referral_load
                logger.info(
                    f"Lab {l.id[:8]} ({getattr(l,'name','?')[:25]}): referral budget frozen at "
                    f"current_subgraph_load={current_referral_load:.0f} "
                    f"(cap_budget={budget_from_cap:.0f}, base={lab_base_demand:.0f}, "
                    f"out_of_subgraph={out_of_subgraph_referral_load:.0f}, "
                    f"util={_pre_redesign_utils.get(l.id,0):.0%}) "
                    f"— no new referrals, existing in-subgraph ones can still be routed away"
                )
            else:
                effective_capacity = budget_from_cap
                logger.debug(
                    f"Lab {l.id[:8]} ({getattr(l,'name','?')[:20]}): raw_cap={lab_capacity:.0f}, "
                    f"util_cap={lab_util_cap:.0%}, base={lab_base_demand:.0f}, "
                    f"colocated={colocated_vol:.0f}, out_of_subgraph={out_of_subgraph_referral_load:.0f}, "
                    f"referral_budget={effective_capacity:.0f} "
                    f"(pre-MILP util={_pre_redesign_utils.get(l.id,0):.0%})"
                )
            assigned_volume = pulp.lpSum(
                state.test_volumes.get(f.id, 0) * x[(f.id, l.id)]
                for f in facilities
                if f.id not in colocated_fac_ids
            )
            prob += assigned_volume <= effective_capacity

        # ---- Structured capacity-budget log (one entry per subgraph solve) ----
        _cap_log_rows = []
        _subgraph_fac_ids_log = {f.id for f in facilities}
        for l in labs:
            _lu = subgraph_lab_utils.get(l.id, 0.0)
            _lc = state.lab_capacity(l.id, self.config.working_days_per_year)
            _lb = float(state.test_volumes.get(l.id, 0))
            _lr = sum(
                float(state.test_volumes.get(ref.facility_id, 0))
                for ref in state.referrals
                if ref.lab_id == l.id
            )
            _colocated = colocated_per_lab.get(l.id, set())
            _cv = sum(float(state.test_volumes.get(fid, 0)) for fid in _colocated)
            _out_of_subgraph = sum(
                float(state.test_volumes.get(ref.facility_id, 0))
                for ref in state.referrals
                if ref.lab_id == l.id
                and ref.facility_id not in _subgraph_fac_ids_log
                and ref.facility_id not in _colocated
            )
            _is_frozen_overloaded = l.id in overloaded_lab_ids
            if _is_frozen_overloaded:
                # Frozen path: cap = in-subgraph referral load (not total demand)
                _in_sg_ref = sum(
                    float(state.test_volumes.get(ref.facility_id, 0))
                    for ref in state.referrals
                    if ref.lab_id == l.id
                    and ref.facility_id in _subgraph_fac_ids_log
                    and ref.facility_id not in _colocated
                )
                _budget = _in_sg_ref
            else:
                _effective_cap = _lc * (1.0 if l.id in labs_with_cap_override else base_max_util_cap)
                _budget = _effective_cap - _cv - _lb - _out_of_subgraph
            _cap_log_rows.append({
                "lab_id": l.id,
                "lab_name": getattr(l, "name", l.id[:8]),
                "pre_milp_utilization": round(_lu, 4),
                "capacity": round(_lc, 0),
                "base_demand": round(_lb, 0),
                "referral_demand_total": round(_lr, 0),
                "colocated_vol": round(_cv, 0),
                "out_of_subgraph_referral_load": round(_out_of_subgraph, 0),
                "referral_budget_for_subgraph": round(_budget, 0),
                "frozen_overloaded": _is_frozen_overloaded,
                "overutil_relief_active": overutil_relief_active,
                "capacity_mode": _labs_y_mode.get(l.id, "scalar"),
            })
        _proj_id = getattr(state, "project_id", None) or "unknown"
        optimization_logger.log(
            _proj_id,
            "MILP_LAB_CAPACITY",
            "subgraph",
            {
                "subgraph_admin_area": subgraph.admin_area,
                "facilities_count": len(facilities),
                "labs_count": len(labs),
                "base_max_util_cap": round(base_max_util_cap, 4),
                "overutil_relief_active": overutil_relief_active,
                "overloaded_labs_count": len(overloaded_lab_ids),
                "labs": _cap_log_rows,
            },
        )
        # ---- End capacity-budget log ----------------------------------------

        # Demand-weighted travel in subgraph (linear in x) — cap vs baseline via slack
        weighted_terms = []
        for f in facilities:
            vol = float(state.test_volumes.get(f.id, 0) or 0)
            if vol <= 0:
                continue
            for l in labs:
                weighted_terms.append(vol * d_mat[(f.id, l.id)] * x[(f.id, l.id)])
        weighted_expr = pulp.lpSum(weighted_terms) if weighted_terms else 0

        preserve_asd = not getattr(self.config, "milp_allow_asd_regression", False)
        asd_slack = None
        if (
            preserve_asd
            and total_subgraph_demand > 0
            and baseline_weighted > 1e-9
        ):
            asd_slack = pulp.LpVariable("asd_slack", lowBound=0)
            eps = 1e-3 + 1e-6 * total_subgraph_demand
            prob += weighted_expr <= baseline_weighted + asd_slack + eps
            # Penalty for violating the ASD preservation constraint is weight-responsive:
            #   dist-focused  (wu ≤ 0.3): high penalty — solver must improve ASD
            #   balanced      (wu ≈ 0.5): standard penalty
            #   util-focused  (wu ≥ 0.6): reduced penalty — allow slight ASD regression
            #                              if needed to balance load
            # overutil-relief mode always uses reduced penalty to route off hot labs.
            if overutil_relief_active:
                M = float(getattr(self.config, "milp_asd_violation_penalty_overutil", 5e4))
                logger.info(
                    f"MILP ASD preservation (overutil-relief): demand-weighted distance ≤ "
                    f"{baseline_weighted:.2f} km·tests (+ slack, reduced penalty M={M:.0g})"
                )
            elif _user_wu >= 0.6:
                # util-focused: allow modest ASD regression to balance load
                M = float(getattr(self.config, "milp_asd_violation_penalty_util", 1e5))
                logger.info(
                    f"MILP ASD preservation (util-focused): demand-weighted distance ≤ "
                    f"{baseline_weighted:.2f} km·tests (+ slack, relaxed penalty M={M:.0g})"
                )
            else:
                M = float(getattr(self.config, "milp_asd_violation_penalty", 5e6))
                logger.info(
                    f"MILP ASD preservation: demand-weighted distance ≤ {baseline_weighted:.2f} km·tests "
                    f"(+ slack, penalty M={M:.0g})"
                )
            prob += obj_main + M * asd_slack
        else:
            if preserve_asd and baseline_weighted <= 1e-9:
                logger.info("MILP ASD preservation skipped (zero baseline weighted distance)")
            prob += obj_main
        
        # Log objective function sample for debugging
        if facilities and labs:
            sample_f = facilities[0]
            sample_costs = []
            for l in labs[:3]:
                if (sample_f.id, l.id) in costs:
                    sample_costs.append((l.id[:8], f"{costs[(sample_f.id, l.id)]:.2f}"))
            logger.debug(f"Sample MILP costs for facility {sample_f.id[:8]}: {sample_costs}")
        
        # Solve (off-loop so sibling subgraphs solve concurrently; semaphore caps cores)
        threads = cbc_threads if cbc_threads is not None else _cbc_threads_per_solve(self.config, 1)
        solver = _resolve_cbc_solver(timeLimit=self.timeout, msg=0, threads=threads)
        try:
            await _solve_prob_in_executor(prob, solver, self._get_cbc_semaphore())
        except pulp.PulpSolverError as e:
            # CBC can crash on a single ill-scaled subgraph (e.g. a large ASD-slack
            # penalty regression). Degrade that ONE subgraph to "unsolved" rather than
            # 500-ing the entire optimization — _solve_redesign_core filters on .solved,
            # so this subgraph simply keeps its baseline assignments while every other
            # subgraph's redesign still applies. We do NOT fall back to nearest-lab greedy
            # here: greedy ignores capacity/capability/overload-freeze and could emit an
            # invalid redesign onto a frozen or incapable lab.
            logger.error(
                "MILP subgraph %s: CBC solver failed (%s) — skipping redesign for this "
                "subgraph, keeping baseline assignments.",
                subgraph.admin_area,
                e,
            )
            return SubgraphSolution(
                subgraph=subgraph,
                assignments={},
                objective_value=0,
                solved=False,
                solve_time_seconds=time.time() - start_time,
            )
        
        # Log solution status
        status_map = {
            pulp.LpStatusOptimal: "Optimal",
            pulp.LpStatusNotSolved: "Not Solved",
            pulp.LpStatusInfeasible: "Infeasible",
            pulp.LpStatusUnbounded: "Unbounded",
            pulp.LpStatusUndefined: "Undefined"
        }
        status_str = status_map.get(prob.status, f"Unknown ({prob.status})")
        obj_value = pulp.value(prob.objective) or 0
        logger.info(f"MILP solve status: {status_str}, objective value: {obj_value:.2f}")
        optimization_logger.log(
            getattr(state, "project_id", None) or "unknown",
            "MILP_SOLVE_STATUS",
            "result",
            {
                "subgraph_admin_area": subgraph.admin_area,
                "status": status_str,
                "objective_value": round(obj_value, 4),
                "facilities": len(facilities),
                "labs": len(labs),
            },
        )
        if asd_slack is not None:
            sv = pulp.value(asd_slack)
            if sv is not None and sv > 1e-2:
                logger.warning(
                    f"MILP subgraph {subgraph.admin_area}: ASD slack = {sv:.4f} km·tests "
                    f"(small demand-weighted distance regression required for feasibility)"
                )
        
        # Log objective breakdown to understand what's driving decisions
        if prob.status == pulp.LpStatusOptimal and facilities and labs:
            total_util_penalty = 0.0
            total_distance_cost = 0.0
            total_action_penalty = 0.0
            
            for f in facilities:
                for l in labs:
                    if x[(f.id, l.id)].value() == 1:
                        cost = costs.get((f.id, l.id), 0)
                        # Decompose cost (rough estimate based on our formula)
                        # Cost = util_penalty + distance_penalty + action_penalty
                        total_distance_cost += cost  # Simplified for now
            
            logger.info(f"📊 MILP objective breakdown: total={obj_value:.2f}")
            logger.info(f"   Facilities in subgraph: {len(facilities)}, Labs: {len(labs)}")
        
        # Extract solution
        assignments = {}
        if prob.status == pulp.LpStatusOptimal:
            for f in facilities:
                for l in labs:
                    if (x[(f.id, l.id)].value() or 0) >= 0.5:
                        assignments[f.id] = l.id
                        break
        
        solve_time = time.time() - start_time
        
        if prob.status == pulp.LpStatusOptimal and assignments:
            logger.info(
                f"MILP subgraph {subgraph.admin_area}: {len(assignments)} optimal assignments "
                f"({len(facilities)} facilities × {len(labs)} labs), obj={pulp.value(prob.objective) or 0:.2f}"
            )
        
        return SubgraphSolution(
            subgraph=subgraph,
            assignments=assignments,
            objective_value=pulp.value(prob.objective) or 0,
            solved=prob.status == pulp.LpStatusOptimal,
            solve_time_seconds=solve_time,
        )
    
    def _solve_greedy_fallback(
        self,
        subgraph: Subgraph,
        state: NetworkState,
        start_time: float
    ) -> SubgraphSolution:
        """Greedy fallback when PuLP not available."""
        import time
        
        assignments = {}
        for f in subgraph.facilities:
            # Find nearest lab with capacity
            best_lab = None
            best_dist = float('inf')
            
            for l in subgraph.labs:
                dist = DistanceCalculator.facility_distance(f, l) or float('inf')
                if dist < best_dist:
                    best_dist = dist
                    best_lab = l
            
            if best_lab:
                assignments[f.id] = best_lab.id
        
        return SubgraphSolution(
            subgraph=subgraph,
            assignments=assignments,
            objective_value=0,
            solved=True,
            solve_time_seconds=time.time() - start_time,
        )
    
    def _resolve_lab_facility(
        self,
        state: NetworkState,
        assignment_reference_state: NetworkState,
        lab_id: str,
    ) -> Optional[Facility]:
        """Resolve lab Facility for distance math (subgraph may omit cross-area labs)."""
        lab = state.labs.get(lab_id)
        if lab:
            return lab
        if assignment_reference_state:
            return assignment_reference_state.labs.get(lab_id)
        return None
    
    def _utilization_benefit_for_reassignment(
        self,
        state: NetworkState,
        old_lab_id: str,
        new_lab_id: str,
    ) -> bool:
        """
        True if moving a typical referral from old_lab toward new_lab helps utilization balance:
        source lab more loaded than destination, or source is over high threshold.
        """
        utils = self._calculate_utilizations(state)
        old_u = utils.get(old_lab_id, 0.0)
        new_u = utils.get(new_lab_id, 0.0)
        threshold_high = float(self.config.utilization_threshold_high)
        # Operational gate: treat labs at 85%+ as "high" so distance-increasing rebalancing
        # is allowed before util hits the configured ceiling (often 1.0).
        gate_high = min(threshold_high, 0.85)
        if old_u >= gate_high:
            return True
        return old_u > new_u + 1e-5
    
    def _merge_solutions(
        self,
        solutions: List[SubgraphSolution],
        state: NetworkState,
        assignment_reference_state: NetworkState,
    ) -> List[RedesignAction]:
        """Merge subproblem solutions into actions (per-action ASD gate, not whole admin area)."""
        actions: List[RedesignAction] = []

        # Partition mode: reference state is the full network while ``state`` is one admin-area
        # slice. A baseline lab absent from state.labs is cross-boundary; the MILP's in-area
        # reassignment for that facility is mandatory (boundary > distance/capacity).
        partition_mode = assignment_reference_state is not state

        def _is_mandatory(old_lab_id: Optional[str]) -> bool:
            # Global runs pass the same object for ref and state → always False here.
            return (
                partition_mode
                and old_lab_id is not None
                and old_lab_id not in state.labs
            )
        
        # Merge assignment maps: reference captures cross-boundary baselines; working ``state``
        # overrides with referrals already applied in this run (redesign iterations).
        current_assignments: Dict[str, str] = {}
        subgraph_ids = set(state.facilities.keys())
        for ref in assignment_reference_state.referrals:
            if ref.facility_id in subgraph_ids:
                current_assignments[ref.facility_id] = ref.lab_id
        for ref in state.referrals:
            if ref.facility_id in subgraph_ids:
                current_assignments[ref.facility_id] = ref.lab_id
        
        distance_improvements = 0
        distance_degradations = 0
        total_distance_change = 0.0
        
        candidates_found = 0
        candidates_rejected = 0
        rejection_reasons: List[str] = []
        per_facility_log: List[Dict] = []
        project_id = getattr(state, "project_id", None) or "unknown"
        
        # Determine which labs need the capacity-relief bypass for the distance gate.
        # Two cases qualify for bypass:
        #   1. Labs already overloaded (>100%) before this MILP iteration.
        #   2. Labs that will become overloaded once the MILP's incoming moves land.
        #      We must allow outgoing "balancing" moves from these labs even if they
        #      don't save distance — otherwise we accept the incoming load but reject
        #      the outgoing relief, which overloads the lab.
        # We do NOT bypass for labs that merely have some departures (the original
        # logic), which let near-zero-km moves pass everywhere in large runs.
        current_utils_pre = self._calculate_utilizations(state)
        already_overloaded: set = {
            lab_id for lab_id, util in current_utils_pre.items() if util > 1.0
        }

        # Compute lab capacities once
        lab_capacities: Dict[str, float] = {}
        for device in state.devices.values():
            cap = device.annual_capacity(self.config.working_days_per_year)
            lab_capacities[device.facility_id] = lab_capacities.get(device.facility_id, 0) + cap

        # Project net volume delta per lab from MILP assignments
        lab_volume_delta: Dict[str, float] = {}
        for solution in solutions:
            for fid, new_lid in solution.assignments.items():
                old_lid = current_assignments.get(fid)
                if old_lid == new_lid:
                    continue
                vol = float(state.test_volumes.get(fid, 0))
                if old_lid:
                    lab_volume_delta[old_lid] = lab_volume_delta.get(old_lid, 0) - vol
                lab_volume_delta[new_lid] = lab_volume_delta.get(new_lid, 0) + vol

        # Labs that will be pushed over capacity by the incoming MILP load
        will_overload: set = set()
        for lab_id, delta in lab_volume_delta.items():
            if delta <= 0:
                continue  # net outgoing — no overload risk
            capacity = lab_capacities.get(lab_id, 0) or 1
            current_util = current_utils_pre.get(lab_id, 0)
            if current_util + (delta / capacity) > 1.0:
                will_overload.add(lab_id)

        overloaded_lab_ids: set = already_overloaded | will_overload
        
        for solution in solutions:
            admin_area = solution.subgraph.admin_area
            for facility_id, new_lab_id in solution.assignments.items():
                facility = state.facilities.get(facility_id)
                old_lab_id = current_assignments.get(facility_id)
                new_lab = self._resolve_lab_facility(
                    state, assignment_reference_state, new_lab_id
                )
                
                # Log every MILP assignment for this subgraph
                log_entry: Dict = {
                    "admin_area": admin_area,
                    "facility_id": facility_id,
                    "facility_name": getattr(facility, "name", None) if facility else None,
                    "milp_assigned_lab_id": new_lab_id,
                    "previous_lab_id": old_lab_id,
                    "status": "no_change",
                }
                
                if not facility:
                    log_entry["status"] = "skip_no_facility_in_state"
                    per_facility_log.append(log_entry)
                    continue
                if not new_lab:
                    log_entry["status"] = "skip_new_lab_not_in_state"
                    per_facility_log.append(log_entry)
                    continue
                if not old_lab_id:
                    log_entry["status"] = "skip_no_prior_referral_in_reference"
                    per_facility_log.append(log_entry)
                    continue
                if old_lab_id == new_lab_id:
                    old_lab_res = self._resolve_lab_facility(
                        state, assignment_reference_state, old_lab_id
                    )
                    old_d = (
                        DistanceCalculator.facility_distance(facility, old_lab_res) or 0
                    ) if old_lab_res else 0
                    log_entry.update(
                        {
                            "status": "unchanged_assignment",
                            "distance_km": round(old_d, 3),
                        }
                    )
                    per_facility_log.append(log_entry)
                    continue
                
                candidates_found += 1
                old_lab = self._resolve_lab_facility(
                    state, assignment_reference_state, old_lab_id
                )
                
                if not old_lab:
                    candidates_rejected += 1
                    msg = f"{facility_id[:8]}: old_lab {old_lab_id[:8] if old_lab_id else 'None'} not resolvable"
                    rejection_reasons.append(msg)
                    log_entry.update({"status": "rejected", "reason": msg})
                    per_facility_log.append(log_entry)
                    continue
                
                old_dist = DistanceCalculator.facility_distance(facility, old_lab) or 0
                new_dist = DistanceCalculator.facility_distance(facility, new_lab) or 0
                distance_change = new_dist - old_dist
                raw_new_km = DistanceCalculator.facility_geodesic_km(facility, new_lab) or 0.0

                # Bypass the min-distance-gain gate ONLY when the old lab is truly
                # overloaded (>100% utilization). Use the pre-computed set.
                move_relieves_overloaded_lab = old_lab_id in overloaded_lab_ids
                # Partition mode: baseline lab outside this slice → MILP in-area move is mandatory.
                mandatory_move = _is_mandatory(old_lab_id)

                min_gain = getattr(self.config, "milp_min_distance_gain_km", None)
                if not mandatory_move and min_gain is not None and float(min_gain) > 0:
                    thr = float(min_gain)
                    if distance_change >= -thr:
                        # Only allow the bypass when the facility is leaving a genuinely
                        # overloaded lab. The old "capacity-swap source" logic let too many
                        # near-zero moves through (any lab with departures got bypass).
                        if move_relieves_overloaded_lab:
                            max_bypass_regression = max(min(old_dist, 25.0), 10.0)
                            bypass_acceptable = distance_change <= max_bypass_regression
                        else:
                            bypass_acceptable = False

                        if bypass_acceptable:
                            logger.info(
                                f"⚡ Overload-relief bypass: accepting {facility_id[:8]} "
                                f"({distance_change:+.2f} km) — source lab {old_lab_id[:8]} overloaded"
                            )
                        else:
                            candidates_rejected += 1
                            msg = (
                                f"{facility_id[:8]}: below min distance gain "
                                f"({distance_change:+.2f} km, threshold={thr} km)"
                            )
                            rejection_reasons.append(msg)
                            log_entry.update(
                                {
                                    "status": "rejected_no_distance_gain",
                                    "reason": msg,
                                    "distance_change_km": round(distance_change, 3),
                                }
                            )
                            per_facility_log.append(log_entry)
                            continue
                
                if distance_change < 0:
                    distance_improvements += 1
                elif distance_change > 0:
                    distance_degradations += 1
                total_distance_change += distance_change
                
                crosses = not facility.is_in_same_admin_area(
                    new_lab,
                    self._effective_boundary_level(),
                )
                
                rationale = f"MILP optimization: reduce distance by {abs(distance_change):.1f}km"
                
                actions.append(
                    RedesignAction(
                        facility_id=facility_id,
                        old_lab_id=old_lab_id,
                        new_lab_id=new_lab_id,
                        distance_change_km=distance_change,
                        new_distance_km=raw_new_km,
                        crosses_admin_boundary=crosses,
                        rationale=rationale,
                    )
                )
                log_entry.update(
                    {
                        "status": "accepted_boundary_mandatory" if mandatory_move else "accepted",
                        "distance_change_km": round(distance_change, 3),
                        "old_dist_km": round(old_dist, 3),
                        "new_dist_km": round(new_dist, 3),
                    }
                )
                per_facility_log.append(log_entry)
        
        logger.info(
            f"MILP merge: {candidates_found} candidates found, {candidates_rejected} rejected, "
            f"{len(actions)} actions created"
        )
        if rejection_reasons:
            logger.warning(f"⚠️  REJECTED CANDIDATES (sample): {rejection_reasons[:8]}")
        
        # Weighted ASD note (informational only — no bulk rejection)
        admin_area_impacts: Dict[str, Dict] = {}
        for action in actions:
            fac = state.facilities.get(action.facility_id)
            if not fac:
                continue
            aa = self._get_admin_area(fac)
            if not aa:
                continue
            if aa not in admin_area_impacts:
                admin_area_impacts[aa] = {"weighted_delta": 0.0, "demand": 0.0}
            vol = state.test_volumes.get(action.facility_id, 0)
            admin_area_impacts[aa]["weighted_delta"] += action.distance_change_km * vol
            admin_area_impacts[aa]["demand"] += vol
        
        for aa, impact in admin_area_impacts.items():
            if impact["demand"] > 0:
                w = impact["weighted_delta"] / impact["demand"]
                if w > 1.0:
                    logger.info(
                        f"MILP merge (info): admin area {aa} net weighted distance change +{w:.2f}km — "
                        f"kept only per-action-approved moves"
                    )
        
        # ── Global capacity enforcement ────────────────────────────────────────
        # The MILP is solved per-subgraph, so two subgraphs can independently
        # assign facilities to the same cross-boundary lab without knowing about
        # each other's assignments. This post-pass enforces a hard global cap:
        # if the combined incoming volume would push a lab over 100%, drop the
        # worst-distance-gain moves until the lab is back within capacity.
        lab_caps: Dict[str, float] = {}
        for device in state.devices.values():
            cap = device.annual_capacity(self.config.working_days_per_year)
            lab_caps[device.facility_id] = lab_caps.get(device.facility_id, 0) + cap

        # Current committed demand per lab (before new actions)
        lab_committed: Dict[str, float] = {}
        for lab_id in state.labs:
            lab_committed[lab_id] = float(state.test_volumes.get(lab_id, 0))
        for ref in state.referrals:
            lab_committed[ref.lab_id] = lab_committed.get(ref.lab_id, 0) + float(state.test_volumes.get(ref.facility_id, 0))

        # Sort proposed actions: distance-saving first (keep best, drop worst when over cap)
        actions.sort(key=lambda a: float(a.distance_change_km))

        cap_enforced: List[RedesignAction] = []
        cap_dropped = 0
        for action in actions:
            new_lab_id = action.new_lab_id
            old_lab_id = action.old_lab_id
            vol = float(state.test_volumes.get(action.facility_id, 0))
            cap = lab_caps.get(new_lab_id, 0)
            mandatory_move = _is_mandatory(old_lab_id)
            if cap > 0:
                projected = lab_committed.get(new_lab_id, 0) + vol
                if projected > cap:
                    if mandatory_move:
                        # Boundary > capacity: accept mandatory in-area assignment anyway.
                        logger.warning(
                            "Boundary-mandatory move overloads lab %s (projected %.0f > cap %.0f) "
                            "— accepted (boundary > capacity)",
                            new_lab_id[:8],
                            projected,
                            cap,
                        )
                    else:
                        cap_dropped += 1
                        logger.info(
                            f"🚫 Cap-guard: dropping {action.facility_id[:8]} → {new_lab_id[:8]} "
                            f"(projected {projected:.0f} > cap {cap:.0f})"
                        )
                        continue
            # Accept — update committed volumes on both labs
            lab_committed[new_lab_id] = lab_committed.get(new_lab_id, 0) + vol
            lab_committed[old_lab_id] = max(0, lab_committed.get(old_lab_id, 0) - vol)
            cap_enforced.append(action)

        if cap_dropped:
            logger.warning(
                f"⚡ Global cap-guard: dropped {cap_dropped} actions that would have overloaded labs; "
                f"{len(cap_enforced)} actions remain"
            )
            actions = cap_enforced
        # ── End global capacity enforcement ───────────────────────────────────

        if actions:
            logger.info(
                f"  Distance improvements: {distance_improvements}, degradations: {distance_degradations}"
            )
            logger.info(f"  Total distance change (sum): {total_distance_change:+.1f}km")
            logger.info(
                f"  Sample actions: {[(a.facility_id[:8], f'{a.distance_change_km:+.1f}km') for a in actions[:5]]}"
            )
            if distance_degradations > distance_improvements:
                logger.warning(
                    f"⚠️  ACCESSIBILITY ALERT: {distance_degradations} facilities worsened vs "
                    f"{distance_improvements} improved (utilization-motivated trade-offs kept)"
                )
        elif candidates_found > 0:
            logger.warning(
                f"⚠️  MILP found {candidates_found} assignment changes but ALL were rejected at merge"
            )
        
        if per_facility_log:
            optimization_logger.log(
                project_id,
                "MILP_FACILITY_ASSIGNMENT",
                "completed",
                {
                    "assignments": per_facility_log[:500],
                    "truncated": len(per_facility_log) > 500,
                    "total_logged": len(per_facility_log),
                },
            )
        
        return actions
