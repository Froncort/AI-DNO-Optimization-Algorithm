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
Optimization goal models.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Literal


def default_expansion_levers(optimization_modes: List[str]) -> Dict[str, bool]:
    """All capacity rungs on when expansion phase is enabled (back-compat for old scenarios)."""
    on = "expansion" in (optimization_modes or [])
    return {
        "test_integration": on,
        "shift_increase": on,
        "add_device": on,
    }


@dataclass
class DynamicScenarioConfig:
    """
    Dynamic scenario configuration - flexible and agent-friendly.
    
    Replaces rigid goal types with configurable optimization parameters.
    
    Attributes:
        optimization_modes: List of enabled optimization methods (relocation, redesign, expansion)
        facility_subset: Optional list of facility/lab IDs to optimize (None = full network)
        allow_conversions: Allow facility-to-lab/hub conversions
        allow_single_device_shifting: Allow shifting devices even if it's the last device in a lab
        budget_constraints: Optional CapEx/OpEx budget limits
        utilization_targets: Target utilization range (min, max)
        accessibility_targets: Target accessibility metrics
        demand_scaling: Admin area -> scale percentage for demand adjustments
        programmatic_constraints: Constraint toggles (enforce_district_boundary, etc.)
        objective_weights: For manual mode - weights for each objective (e.g., {"utilization": 0.6, "accessibility": 0.3, "cost": 0.1})
    """
    
    # Optimization behavior
    optimization_modes: List[str] = field(default_factory=lambda: ["redesign"])
    facility_subset: Optional[List[str]] = None  # None = full network optimization
    optimize_by_admin_area: bool = False  # If True, run optimization separately per geographic partition
    # Which boundary to use when optimize_by_admin_area is True (MILP subgraph + state filter).
    optimize_admin_area_level: Literal["admin_area_1", "admin_area_2"] = "admin_area_1"
    
    # Permissions
    allow_conversions: bool = True
    allow_single_device_shifting: bool = True  # Can shift 1-device labs
    
    # Budget constraints
    budget_constraints: Optional[Dict[str, float]] = None  # {"max_capex": X, "max_opex_annual": Y}
    
    # Target metrics
    utilization_targets: Optional[Dict[str, float]] = None  # {"min": 0.4, "max": 0.8}
    accessibility_targets: Optional[Dict[str, float]] = None
    
    # Demand modifications
    demand_scaling: Optional[Dict[str, float]] = None  # admin_area -> scale %
    
    # Programmatic constraints (Enhancement #9)
    programmatic_constraints: Optional[Dict[str, bool]] = None  # {"enforce_district_boundary": True, ...}
    
    # Objective weights for solvers (NEW)
    objective_weights: Optional[Dict[str, float]] = None  # {"utilization": 0.6, "accessibility": 0.3, "cost": 0.1}

    # Capacity ladder rungs (integration / shift / device) — not peer optimization_modes entries
    expansion_levers: Optional[Dict[str, bool]] = None
    
    def __post_init__(self) -> None:
        if self.expansion_levers is None:
            self.expansion_levers = default_expansion_levers(self.optimization_modes)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return {
            "optimization_modes": self.optimization_modes,
            "facility_subset": self.facility_subset,
            "optimize_by_admin_area": self.optimize_by_admin_area,
            "optimize_admin_area_level": self.optimize_admin_area_level,
            "allow_conversions": self.allow_conversions,
            "allow_single_device_shifting": self.allow_single_device_shifting,
            "budget_constraints": self.budget_constraints,
            "utilization_targets": self.utilization_targets,
            "accessibility_targets": self.accessibility_targets,
            "demand_scaling": self.demand_scaling,
            "programmatic_constraints": self.programmatic_constraints,
            "objective_weights": self.objective_weights,
            "expansion_levers": self.expansion_levers,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicScenarioConfig":
        """Create from dictionary (JSON deserialization)."""
        import logging

        logger = logging.getLogger(__name__)
        _lvl = data.get("optimize_admin_area_level", "admin_area_1")
        if _lvl not in ("admin_area_1", "admin_area_2"):
            _lvl = "admin_area_1"
        if "optimization_modes" not in data or data.get("optimization_modes") is None:
            logger.warning(
                "optimization_modes missing or null in DynamicScenarioConfig JSON — "
                "use OssGoal.to_scenario_config() or pass explicit modes"
            )
            raise ValueError(
                "optimization_modes is required in DynamicScenarioConfig JSON; "
                "use OssGoal presets or pass explicit modes (OSS supports redesign-only open_referral)"
            )
        modes = data["optimization_modes"]
        levers = data.get("expansion_levers")
        if levers is None:
            levers = default_expansion_levers(modes)
        return cls(
            optimization_modes=modes,
            facility_subset=data.get("facility_subset"),
            optimize_by_admin_area=data.get("optimize_by_admin_area", False),
            optimize_admin_area_level=_lvl,
            allow_conversions=data.get("allow_conversions", True),
            allow_single_device_shifting=data.get("allow_single_device_shifting", True),
            budget_constraints=data.get("budget_constraints"),
            utilization_targets=data.get("utilization_targets"),
            accessibility_targets=data.get("accessibility_targets"),
            demand_scaling=data.get("demand_scaling"),
            programmatic_constraints=data.get("programmatic_constraints"),
            objective_weights=data.get("objective_weights"),
            expansion_levers=levers,
        )


@dataclass
class OptimizationGoal:
    """
    Base optimization goal.
    
    Attributes:
        goal_type: Type of goal (capacity_increase, demand_projection, etc.)
        description: Human-readable goal description
        target_metric: Goal-specific target value
        constraints: Additional constraints
        optimization_mode: Mode for optimization (utilization|accessibility|capacity|cost|hybrid)
        objective_weights: For hybrid mode - weights for each objective (must sum to 1.0)
    """
    
    goal_type: str = "generic"
    description: str = ""
    target_metric: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    optimization_mode: str = "utilization"  # utilization|accessibility|capacity|cost|hybrid
    objective_weights: Optional[Dict[str, float]] = None  # For hybrid mode
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_type": self.goal_type,
            "description": self.description,
            "target_metric": self.target_metric,
            "constraints": self.constraints,
            "optimization_mode": self.optimization_mode,
            "objective_weights": self.objective_weights,
        }

    def get_optimization_modes(self) -> List[str]:
        """
        Return which RRE steps this goal requires.
        
        Returns:
            List of optimization modes: ["relocation", "redesign", "expansion"]
        """
        # Default: all steps
        return ["relocation", "redesign", "expansion"]


@dataclass
class CapacityGoal(OptimizationGoal):
    """
    Capacity increase goal.
    
    Example: "Increase TB testing capacity to 15,000 tests/year"
    
    Attributes:
        target_annual_tests: Target annual test capacity
        current_annual_tests: Current annual test capacity (baseline)
    """
    
    target_annual_tests: int = 0
    current_annual_tests: int = 0
    
    def __post_init__(self):
        self.goal_type = "capacity_increase"
        self.target_metric = {
            "target_annual_tests": self.target_annual_tests,
            "current_annual_tests": self.current_annual_tests,
        }
    
    @property
    def capacity_gap(self) -> int:
        """Calculate the capacity gap to be filled."""
        return max(0, self.target_annual_tests - self.current_annual_tests)
    
    @property
    def growth_ratio(self) -> float:
        """Calculate the growth ratio."""
        if self.current_annual_tests == 0:
            return float('inf')
        return self.target_annual_tests / self.current_annual_tests
    
    def get_optimization_modes(self) -> List[str]:
        """
        After demand scale: relocation + MILP redesign redistributes load; expansion closes gap.
        """
        return ["relocation", "redesign", "expansion"]


@dataclass
class DemandProjectionGoal(OptimizationGoal):
    """
    Demand projection goal.
    
    Example: "Prepare for 20% demand growth over 3 years"
    
    Attributes:
        demand_increase_percent: Percentage increase in demand
        projection_years: Number of years for projection
    """
    
    demand_increase_percent: float = 0.0
    projection_years: int = 3
    
    def __post_init__(self):
        self.goal_type = "demand_projection"
        self.target_metric = {
            "demand_increase_percent": self.demand_increase_percent,
            "projection_years": self.projection_years,
        }
    
    @property
    def annual_growth_rate(self) -> float:
        """Calculate compound annual growth rate."""
        if self.projection_years <= 0:
            return 0.0
        total_multiplier = 1 + (self.demand_increase_percent / 100)
        return (total_multiplier ** (1 / self.projection_years)) - 1
    
    @property
    def total_multiplier(self) -> float:
        """Calculate total demand multiplier."""
        return 1 + (self.demand_increase_percent / 100)
    
    def get_optimization_modes(self) -> List[str]:
        """
        Demand projection goals need redesign and expansion.
        
        Returns:
            ["redesign", "expansion"] - Handle future demand
        """
        return ["redesign", "expansion"]


@dataclass
class CoverageGoal(OptimizationGoal):
    """
    Coverage / geographic-access goal — default solver posture is relocation + redesign (RR),
    aligned with SKILL and domain baseline scenarios. Expansion is handled by registry hints
    or a separate ``full_rre`` comparator row unless explicitly requested.

    Example: "Ensure all Level 4 facilities within 50km of a lab"
    
    Attributes:
        target_coverage_percent: Target population coverage
        max_distance_km: Maximum acceptable distance to lab
        target_facility_tiers: Facility tiers to prioritize
    """
    
    target_coverage_percent: float = 100.0
    max_distance_km: float = 50.0
    target_facility_tiers: Optional[list] = None
    
    def __post_init__(self):
        self.goal_type = "coverage_expansion"
        self.target_metric = {
            "target_coverage_percent": self.target_coverage_percent,
            "max_distance_km": self.max_distance_km,
            "target_facility_tiers": self.target_facility_tiers or [],
        }
    
    def get_optimization_modes(self) -> List[str]:
        """
        Relocation + redesign only (no expansion phase) unless a DynamicScenarioConfig
        path sets ``optimization_modes`` with expansion via hints.

        Returns:
            ["relocation", "redesign"]
        """
        return ["relocation", "redesign"]


@dataclass
class UtilizationBalanceGoal(OptimizationGoal):
    """
    Utilization balance goal.
    
    Example: "Reduce overutilization to <80% across all labs"
    
    Attributes:
        max_utilization_target: Target maximum utilization
        min_utilization_target: Target minimum utilization
        force_balance: If True, aggressively try to balance even if distance/cost constraints violated
        allow_expansion: If True, allow adding capacity if balancing via relocation/redesign fails
    """
    
    max_utilization_target: float = 0.80
    min_utilization_target: float = 0.40
    force_balance: bool = True  # Default to aggressive balancing
    allow_expansion: bool = True  # Allow expansion if R&R can't fix it
    
    def __post_init__(self):
        self.goal_type = "utilization_balance"
        self.target_metric = {
            "max_utilization_target": self.max_utilization_target,
            "min_utilization_target": self.min_utilization_target,
            "force_balance": self.force_balance,
            "allow_expansion": self.allow_expansion,
        }
    
    def get_optimization_modes(self) -> List[str]:
        """
        Utilization balance goals use all RRE steps to achieve balance.
        
        Returns:
            ["relocation", "redesign", "expansion"] - Try everything to balance
        """
        if self.allow_expansion:
            return ["relocation", "redesign", "expansion"]
        else:
            return ["relocation", "redesign"]


@dataclass  
class DevicePlacementGoal(OptimizationGoal):
    """
    Device placement goal.
    
    Example: "I have 5 new GeneXpert machines. Where should I place them?"
    
    Attributes:
        device_type: Type of device to place
        quantity: Number of devices to place
        allow_conversions: Allow converting facilities to labs when the
            best placement site is not already a lab.
        prefer_existing_labs: Score existing labs higher than plain facilities.
        prefer_facility_sites: Opposite bias — penalise existing labs and boost
            non-lab facilities as placement sites (decentralised / greenfield hubs).
            Mutually exclusive in the UI with strong \"prefer labs\"; if both are
            set, prefer_facility_sites wins.
        enable_redesign: Always True in practice — device placement uses MILP redesign
            after each placed device (structural search). Kept for API compatibility.
        enable_relocation: After placing, allow device relocations as well.
        per_admin_area_quantities: Optional district-level quotas (admin_area_1 -> count).
            When set with positive entries, placement is partitioned by admin area.
        placement_items: Optional list of {device_type, quantity, per_admin_area_quantities?} to place
            several catalogue types in one goal. Processed in order. Each row may include
            per_admin_area_quantities (admin_area_1 -> count). For a single row, goal-level
            per_admin_area_quantities is used when the row omits district splits.
    """
    
    device_type: str = ""
    quantity: int = 0
    allow_conversions: bool = True
    prefer_existing_labs: bool = True
    prefer_facility_sites: bool = False
    enable_redesign: bool = True
    enable_relocation: bool = False
    per_admin_area_quantities: Optional[Dict[str, int]] = None
    placement_items: Optional[List[Dict[str, Any]]] = None
    
    def __post_init__(self):
        self.goal_type = "device_placement"
        # Always use expansion + MILP redesign loop (one device per structural round).
        self.enable_redesign = True
        # Geographic allocation — access-first scoring for placement rounds
        if not self.objective_weights:
            self.objective_weights = {
                "accessibility": 0.6,
                "utilization": 0.3,
                "cost": 0.1,
            }
        self.target_metric = {
            "device_type": self.device_type,
            "quantity": self.quantity,
            "allow_conversions": self.allow_conversions,
            "prefer_existing_labs": self.prefer_existing_labs,
            "prefer_facility_sites": self.prefer_facility_sites,
            "enable_redesign": self.enable_redesign,
            "enable_relocation": self.enable_relocation,
            "per_admin_area_quantities": self.per_admin_area_quantities or {},
            "placement_items": self.placement_items or [],
        }

    def has_actionable_spec(self) -> bool:
        """At least one placement row with non-empty device_type and positive quantity."""
        items = [x for x in (self.placement_items or []) if isinstance(x, dict)]
        if items:
            for it in items:
                dt = str(it.get("device_type") or "").strip()
                try:
                    qt = int(it.get("quantity") or 0)
                except (TypeError, ValueError):
                    qt = 0
                if dt and qt > 0:
                    return True
            return False
        dt = str(self.device_type or "").strip()
        try:
            qt = int(self.quantity or 0)
        except (TypeError, ValueError):
            qt = 0
        return bool(dt and qt > 0)

    @property
    def total_placement_quantity(self) -> int:
        """Total devices to place (all types), for structural-search round limits."""
        items = self.placement_items
        if items:
            return sum(int(x.get("quantity") or 0) for x in items if isinstance(x, dict))
        return int(self.quantity or 0)

    @property
    def total_structural_search_rounds(self) -> int:
        """
        Upper bound on placement iterations (one round per device placed).
        Uses per-row or legacy per-area quota sums when set, else row quantities.
        """
        items = [x for x in (self.placement_items or []) if isinstance(x, dict)]

        def _pos_sum(pa: Optional[Dict[str, int]]) -> int:
            if not pa:
                return 0
            return sum(int(v) for v in pa.values() if (v or 0) > 0)

        if not items:
            pa = self.per_admin_area_quantities or {}
            q = int(self.quantity or 0)
            if _pos_sum(pa) > 0:
                return max(1, max(q, _pos_sum(pa)))
            return max(1, q)

        total = 0
        for it in items:
            dt = (it.get("device_type") or "").strip()
            qt = int(it.get("quantity") or 0)
            if not dt or qt <= 0:
                continue
            pa = it.get("per_admin_area_quantities") or {}
            if len(items) == 1 and _pos_sum(pa) == 0:
                pa = self.per_admin_area_quantities or {}
            if _pos_sum(pa) > 0:
                total += _pos_sum(pa)
            else:
                total += qt
        return max(1, total)
    
    def get_optimization_modes(self) -> List[str]:
        """
        Expansion + MILP redesign always (each placement hypothesis is scored after redesign).
        Relocation is optional.
        """
        modes = ["expansion", "redesign"]
        if self.enable_relocation:
            modes.insert(0, "relocation")
        return modes


@dataclass
class BudgetConstrainedGoal(OptimizationGoal):
    """
    Budget-constrained optimization goal.
    
    Example: "Optimize within ₹10,00,000 budget"
    
    Attributes:
        max_budget: Maximum budget available
        currency: Currency code (INR, USD, etc.)
        priority: What to prioritize (accessibility, utilization, cost)
    """
    
    max_budget: float = 0.0
    currency: str = "INR"
    priority: str = "accessibility"  # accessibility | utilization | cost
    
    def __post_init__(self):
        self.goal_type = "budget_constrained"
        self.target_metric = {
            "max_budget": self.max_budget,
            "currency": self.currency,
            "priority": self.priority,
        }
    
    def get_optimization_modes(self) -> List[str]:
        """
        Budget constrained goals need all RRE steps.
        
        Returns:
            ["relocation", "redesign", "expansion"] - All steps within budget
        """
        return ["relocation", "redesign", "expansion"]


@dataclass
class OptimizationConstraints:
    """
    Translated optimization constraints from a goal.
    
    Produced by GoalTranslator from OptimizationGoal.
    """
    
    # Capacity constraints
    min_total_capacity: int = 0
    capacity_gap: int = 0
    implied_device_additions: int = 0
    
    # Operational constraints
    allow_expansions: bool = True
    allow_relocations: bool = True
    allow_conversions: bool = True
    
    # Optimization focus
    optimize_for: str = "balance"  # "efficiency", "capacity", "cost", "balance"
    
    # Budget constraints
    max_capex: Optional[float] = None
    max_opex_annual: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_total_capacity": self.min_total_capacity,
            "capacity_gap": self.capacity_gap,
            "implied_device_additions": self.implied_device_additions,
            "allow_expansions": self.allow_expansions,
            "allow_relocations": self.allow_relocations,
            "allow_conversions": self.allow_conversions,
            "optimize_for": self.optimize_for,
            "max_capex": self.max_capex,
            "max_opex_annual": self.max_opex_annual,
        }
