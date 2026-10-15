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
Engine configuration model (database-driven).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Any, Optional, Set, List, Tuple


def _default_infra_score_map() -> Dict[str, float]:
    return {
        "LEVEL_1": 1.0,
        "LEVEL_2": 0.9,
        "LEVEL_3": 0.7,
        "LEVEL_4": 0.4,
        "LEVEL_5": 0.2,
    }


def _default_infra_device_limits() -> Dict[str, int]:
    return {
        "LEVEL_1": 50,
        "LEVEL_2": 20,
        "LEVEL_3": 10,
        "LEVEL_4": 4,
        "LEVEL_5": 2,
    }


@dataclass
class EngineConfig:
    """
    DNO Engine configuration loaded from database.
    
    Replaces YAML-based configuration with database-driven settings,
    allowing frontend modification without code changes.
    
    All settings have sensible defaults that can be overridden
    per-project in the engine_config table.
    """
    
    project_id: str
    
    # ──────────────────────────────────────────────────────────────────────────
    # Operational Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    working_days_per_year: int = 300
    """Number of operational days per year for capacity calculation."""
    
    default_shifts_per_day: int = 1
    """Default shifts if not specified at device level."""

    max_shifts_per_day: int = 3
    """Ceiling for operational_shifts / shift lever (hydrated from situational_rules)."""
    
    utilization_threshold_low: Decimal = Decimal("0.40")
    """Below this = underutilized (default: 40%)."""
    
    utilization_threshold_high: Decimal = Decimal("0.95")
    """Above this = overutilized (default: 95%)."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Admin Boundary Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    # Default off: MILP may assign across admin areas; neighbor labs stay injectable.
    admin_boundary_enabled: bool = False
    """When True, MILP respects district boundaries and post-pass repairs cross-boundary referrals."""
    
    admin_boundary_level: str = "admin_area_1"
    """
    Geographic level for MILP subgraphs, cross-boundary flags, and related logic.
    Default ``admin_area_1`` (district-scale) so labs in the same district are
    co-optimized; use ``admin_area_2`` for finer (block/sub-district) partitioning.
    """
    
    allow_cross_boundary: bool = False
    """Allow optimization across admin boundaries."""
    
    cross_boundary_penalty: Decimal = Decimal("1.5")
    """Cost multiplier for cross-boundary assignments."""
    
    intra_boundary_preference_weight: Decimal = Decimal("1.2")
    """Preference weight for staying within boundaries."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Conversion Policy
    # ──────────────────────────────────────────────────────────────────────────
    
    conversion_preference: str = "lab_to_lab_first"
    """
    Conversion policy:
    - 'lab_to_lab_first': Prioritize relocation between existing labs
    - 'conversion_allowed': Equal consideration for both
    - 'conversion_preferred': Favor facility→lab conversion
    """
    
    conversion_cost_penalty: Decimal = Decimal("1.2")
    """Cost multiplier for conversion vs relocation."""
    
    min_confidence_threshold: Decimal = Decimal("0.7")
    """Minimum confidence score to allow conversion."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Cost Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    annualization_period_years: int = 5
    """Period to spread startup costs."""
    
    cost_per_km: Decimal = Decimal("50")
    """Transport cost per kilometer."""
    
    fixed_handling_cost: Decimal = Decimal("5000")
    """Fixed cost per relocation."""
    
    transport_model_weeks_per_year: int = 50
    """
    Weeks per year used in the sample-transport cost formula: if HF MaxFreq × W
    is less than HF Total Demand, volume = MaxFreq × W; otherwise volume = demand.
    """

    transport_max_freq_per_week_override: Optional[int] = None
    """
    When set, clamps every facility's max ship frequency to this value before
    applying the transport cost formula. Useful to standardise across datasets
    (e.g. set to 3 to match an external model's uniform-3-trips assumption).
    Leave as None to use per-facility actual frequencies from the data.
    """

    sample_transport_mode_ids: Optional[Set[str]] = None
    """
    Scenario-only: set of selected mode ids when not using all modes.
    Used with sample_transport_mode_allocations for blended recosting.
    None means all modes (baseline and scenario 'use all').
    """

    sample_transport_mode_allocations: Optional[List[Tuple[str, float]]] = None
    """
    Scenario-only blended costing: list of (mode_id, fraction) summing to 1.
    When set, every referral leg is re-costed using catalog cpk/speed for these modes.
    """

    sample_transport_mode_catalog: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    """Project ref_transport_modes keyed by id: {mode, cost_per_km, speed_kmph}."""

    default_sample_transport_mode_id: Optional[str] = None
    """Default mode id for legs without an explicit route mode (scenario exports)."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Goal-Driven Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    goal_driven_enabled: bool = True
    """Enable goal-driven optimization."""
    
    demand_projection_years: int = 3
    """Default years for demand projection."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Convergence Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    max_iterations: int = 30
    """Maximum optimization loop iterations."""
    
    utilization_tolerance: Decimal = Decimal("0.05")
    """Convergence tolerance for utilization changes."""
    
    cost_improvement_threshold: Decimal = Decimal("0.02")
    """Minimum cost improvement to continue iterating."""
    
    min_convergence_iterations: int = 3
    """Minimum iterations before checking convergence."""
    
    convergence_tolerance: float = 0.01
    """Tolerance for metric changes to declare convergence."""
    
    oscillation_detection_window: int = 5
    """Number of iterations to check for oscillation patterns."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # Action Penalty Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    action_penalty_weight: float = 75.0
    """Penalty for changing existing referrals (prevents micro-optimizations)."""
    
    # ──────────────────────────────────────────────────────────────────────────
    # MILP Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    milp_enabled: bool = True
    """
    Enables referral redesign MILP and Phase 3.5 scored expansion (when configured).
    Device relocation always attempts MILP first regardless of this flag; greedy relocation
    is only a fallback if MILP yields no moves.
    """
    
    milp_timeout_seconds: int = 360
    """Timeout per MILP subproblem."""
    
    milp_max_subgraph_size: int = 300
    """Maximum facilities + labs per MILP subgraph (Pune district ~241)."""

    milp_max_parallel_solves: int = 4
    """Max CBC subgraph solves run concurrently (capped to CPU cores at runtime)."""
    
    milp_allow_asd_regression: bool = False
    """
    When False (default), MILP adds a high-penalty slack so demand-weighted travel
    in each admin subgraph does not exceed the pre-optimization baseline (except
    when infeasible, in which case slack absorbs minimal regression).
    Set True to allow trade-offs that worsen average distance for utilization.
    """
    
    milp_asd_violation_penalty: float = 5_000_000.0
    """Objective multiplier on ASD slack; raise if distance still regresses too often."""

    milp_min_distance_gain_km: Optional[float] = 1.0
    """
    Minimum facility-to-lab distance improvement (km) required to emit a redesign action.
    Default ``1.0`` drops sub-km noise moves. Set to ``0`` or ``None`` to disable the filter.
    """
    
    # ──────────────────────────────────────────────────────────────────────────
    # Relocation Settings
    # ──────────────────────────────────────────────────────────────────────────

    max_relocation_distance_km: float = 150.0
    """Maximum distance (km) for device relocations."""

    distance_mode: str = "aerial"
    """
    Distance calculation mode:
    - 'aerial': Haversine × 1.4 circuitry factor (default)
    - 'road': Haversine × 1.6 road approximation factor (higher circuitry for road networks)
    """

    max_lab_utilization: float = 0.95
    """
    Hard cap on lab utilization in MILP constraints (assigned demand / capacity).
    Default 0.95 — prevents packing labs to 100% so load spreads across sites.
    """

    scored_expansion_enabled: bool = True
    """
    Legacy flag — kept for backward compatibility.
    Phase 3.5 facility-to-lab conversion now runs automatically whenever
    expansion + redesign modes are both active and MILP is enabled.
    """

    scored_expansion_candidates: int = 5
    """Max facility conversion candidates fetched (pre-ranked by infra×proximity) for structural search."""

    milp_conversion_full_score_topk: int = 3
    """
    How many of the pre-ranked conversion candidates to run a full MILP redesign on.
    Candidates are already sorted best-first by get_facility_conversion_candidates, so
    evaluating only the top-K captures the best outcome with far fewer heavy solves.
    Must be ≤ scored_expansion_candidates; clamped at runtime if larger.
    """

    max_conversion_iterations: int = 2
    """Outer iterations for unified structural search (relocation + expansion + conversion)."""

    max_capacity_ladder_rounds: int = 2
    """Structural rounds for non-placement capacity ladder cascade (integration → … → conversion)."""

    min_conversion_improvement: float = 0.005
    """Minimum composite score gain required to commit a structural action (see _score_expansion_hypothesis)."""

    # ──────────────────────────────────────────────────────────────────────────
    # Expansion / placement scoring (solver tuning — optional DB JSON overrides via situational_rules.placement_solver)
    # ──────────────────────────────────────────────────────────────────────────

    accessibility_normalizer_km: float = 50.0
    expansion_target_utilization: float = 0.80
    """Post-overutil relief target utilization when estimating capacity gap."""

    infra_score_map: Dict[str, float] = field(default_factory=_default_infra_score_map)
    infra_device_limits: Dict[str, int] = field(default_factory=_default_infra_device_limits)

    expansion_weight_headroom: float = 0.35
    expansion_weight_infrastructure: float = 0.35
    expansion_weight_cost_efficiency: float = 0.20
    expansion_weight_accessibility: float = 0.10

    placement_weight_utilization: float = 0.50
    placement_weight_infrastructure: float = 0.30
    placement_weight_affinity: float = 0.20
    placement_weight_accessibility: float = 0.00

    auto_regional_preallocation_enabled: bool = True
    placement_diversity_penalty_per_device: float = 0.0
    placement_score_cache_force_rebuild: bool = False
    """Deprecated/inert: retained for DB parity; placement no longer fingerprints a score cache."""

    placement_asd_proxy_for_optimize: bool = True
    """
    When True, placement enumeration (discovery API and device-placement rounds) uses a
    demand-weighted distance proxy instead of classic accessibility for the accessibility signal.
    """

    placement_asd_proxy_admin_local_only: bool = True
    """
    When True and the candidate site has admin_area_1, only referrals whose facility
    is in the same admin_area_1 are included in the proxy (cheaper / more locality-aware).
    """

    placement_optimize_proxy_inject_weight: float = 0.14
    """
    When placement ASD proxy is on: shrink util/infra/affinity weights by (1 - this)
    and allocate this fraction to accessibility using the ASD proxy (so proxy matters
    even when placement_weight_accessibility is 0).
    """

    # ──────────────────────────────────────────────────────────────────────────
    # Facility conversion scoring (non-lab candidates in placement discovery)
    # Weights must sum to 1.0 — referral vs regional are separate stress signals.
    # ──────────────────────────────────────────────────────────────────────────

    facility_weight_referral_pressure: float = 0.28
    facility_weight_regional_utilization: float = 0.22
    facility_weight_infrastructure: float = 0.18
    facility_weight_accessibility: float = 0.17
    facility_weight_distance_gap: float = 0.15

    facility_referral_pressure_threshold: float = 0.5
    facility_regional_pressure_threshold: float = 0.5
    facility_distance_gap_normalizer_km: float = 100.0

    facility_min_confidence_soft_floor: float = 0.40
    facility_confidence_soft_multiplier: bool = True
    facility_show_when_overutil: bool = False

    device_placement_single_hypothesis_milp: bool = True
    """
    When True and the goal is DevicePlacementGoal, structural search runs at most one
    MILP redesign per round on the ranked ExpansionAction and skips the multi-facility
    conversion candidate sweep.
    """

    device_placement_max_milp_candidates: int = 1
    """
    Reserved for a future multi-candidate device placement mode. No effect while
    device_placement_single_hypothesis_milp is True (v1 default).
    """

    device_placement_exploration_candidates: int = 0
    """
    Reserved for future exploration on top-K ranked sites. No effect in v1 default.
    """

    device_placement_conversion_fallback_enabled: bool = False
    """
    When True and single-hypothesis mode is off, allows demand-ranked facility fallback
    in structural conversion scoring (legacy behavior). Default False — fallback was harmful.
    """

    # ──────────────────────────────────────────────────────────────────────────
    # Flexible JSON settings
    # ──────────────────────────────────────────────────────────────────────────
    
    situational_rules: Dict[str, str] = field(default_factory=dict)
    """
    Context-specific conversion overrides.
    Example: {"high_demand_areas": "conversion_preferred", "urban_areas": "lab_to_lab_first"}
    """
    
    device_compatibility: Dict[str, Any] = field(default_factory=dict)
    """
    Device tier requirements and infrastructure needs.
    Example: {"tier_requirements": {"GX 4": 4}, "infrastructure": {"GX 4": ["AC", "POWER"]}}
    """

    facility_level_mapping: Dict[str, Any] = field(default_factory=dict)
    """
    Maps facility tier keys (e.g. \"1\"..\"5\", or full tier strings) to display buckets.
    Example: {\"1\": {\"label\": \"PHC\", \"color\": \"#0d9488\"}, \"3\": {\"label\": \"DMC\", \"color\": \"#6366f1\"}}
    """

    # ──────────────────────────────────────────────────────────────────────────
    # Referral Generation Settings
    # ──────────────────────────────────────────────────────────────────────────
    
    min_referral_threshold: int = 50
    """Minimum referrals to consider historical data sufficient."""
    
    max_referrals_per_facility: int = 3
    """Maximum labs a facility can be linked to."""
    
    referral_distance_weight: Decimal = Decimal("0.4")
    """Weight for distance in referral scoring."""
    
    referral_capacity_weight: Decimal = Decimal("0.3")
    """Weight for capacity in referral scoring."""
    
    referral_admin_weight: Decimal = Decimal("0.2")
    """Weight for admin alignment in referral scoring."""
    
    referral_tier_weight: Decimal = Decimal("0.1")
    """Weight for tier compatibility in referral scoring."""

    def __post_init__(self) -> None:
        ew = (
            self.expansion_weight_headroom
            + self.expansion_weight_infrastructure
            + self.expansion_weight_cost_efficiency
            + self.expansion_weight_accessibility
        )
        if abs(ew - 1.0) > 1e-6:
            raise ValueError(
                f"expansion_weight_* fields must sum to 1.0 (±1e-6), got {ew:.8f} "
                f"(headroom={self.expansion_weight_headroom}, infrastructure="
                f"{self.expansion_weight_infrastructure}, cost_efficiency="
                f"{self.expansion_weight_cost_efficiency}, accessibility="
                f"{self.expansion_weight_accessibility})"
            )

        core = (
            self.placement_weight_utilization
            + self.placement_weight_infrastructure
            + self.placement_weight_affinity
        )
        pw = core + self.placement_weight_accessibility
        if abs(self.placement_weight_accessibility) < 1e-12:
            if abs(core - 1.0) > 1e-6:
                raise ValueError(
                    f"When placement_weight_accessibility is 0, util+infra+affinity must sum to "
                    f"1.0 (±1e-6), got {core:.8f}"
                )
        elif abs(pw - 1.0) > 1e-6:
            raise ValueError(
                f"placement_weight_* fields must sum to 1.0 (±1e-6), got {pw:.8f}"
            )

        fw = (
            self.facility_weight_referral_pressure
            + self.facility_weight_regional_utilization
            + self.facility_weight_infrastructure
            + self.facility_weight_accessibility
            + self.facility_weight_distance_gap
        )
        if abs(fw - 1.0) > 1e-6:
            raise ValueError(
                f"facility_weight_* fields must sum to 1.0 (±1e-6), got {fw:.8f}"
            )

        if (
            self.expansion_target_utilization <= 0
            or self.expansion_target_utilization >= 1.0
        ):
            raise ValueError(
                f"expansion_target_utilization must be in (0,1), got "
                f"{self.expansion_target_utilization}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "project_id": self.project_id,
            "operational": {
                "working_days_per_year": self.working_days_per_year,
                "default_shifts_per_day": self.default_shifts_per_day,
                "max_shifts_per_day": self.max_shifts_per_day,
                "utilization_threshold_low": float(self.utilization_threshold_low),
                "utilization_threshold_high": float(self.utilization_threshold_high),
            },
            "admin_boundaries": {
                "enabled": self.admin_boundary_enabled,
                "level": self.admin_boundary_level,
                "allow_cross_boundary": self.allow_cross_boundary,
                "cross_boundary_penalty": float(self.cross_boundary_penalty),
            },
            "conversion_policy": {
                "preference": self.conversion_preference,
                "cost_penalty": float(self.conversion_cost_penalty),
                "min_confidence": float(self.min_confidence_threshold),
                "situational_rules": self.situational_rules,
            },
            "costs": {
                "annualization_period_years": self.annualization_period_years,
                "cost_per_km": float(self.cost_per_km),
                "fixed_handling_cost": float(self.fixed_handling_cost),
                "transport_model_weeks_per_year": self.transport_model_weeks_per_year,
                "transport_max_freq_per_week_override": self.transport_max_freq_per_week_override,
                "transport_max_freq_per_week_by_admin_area_2": (
                    (self.situational_rules or {}).get(
                        "transport_max_freq_per_week_by_admin_area_2"
                    )
                    or {}
                ),
            },
            "goal_driven": {
                "enabled": self.goal_driven_enabled,
                "demand_projection_years": self.demand_projection_years,
            },
            "convergence": {
                "max_iterations": self.max_iterations,
                "utilization_tolerance": float(self.utilization_tolerance),
                "cost_improvement_threshold": float(self.cost_improvement_threshold),
            },
            "milp": {
                "enabled": self.milp_enabled,
                "timeout_seconds": self.milp_timeout_seconds,
                "max_subgraph_size": self.milp_max_subgraph_size,
                "min_distance_gain_km": self.milp_min_distance_gain_km,
            },
            "referrals": {
                "min_referral_threshold": self.min_referral_threshold,
                "max_referrals_per_facility": self.max_referrals_per_facility,
                "distance_weight": float(self.referral_distance_weight),
                "capacity_weight": float(self.referral_capacity_weight),
                "admin_weight": float(self.referral_admin_weight),
                "tier_weight": float(self.referral_tier_weight),
            },
            "relocation": {
                "max_distance_km": float(self.max_relocation_distance_km),
            },
            "facility_level_mapping": self.facility_level_mapping,
            "device_capacity": self._device_capacity_block(),
        }

    def _device_capacity_block(self) -> Dict[str, Any]:
        from dno_core.domain.mad_config import normalize_mad_device_caps

        sr = self.situational_rules or {}
        return {
            "tier_limits": dict(self.infra_device_limits),
            "mad_device_caps": normalize_mad_device_caps(sr.get("mad_device_caps")),
            "test_wise_enforcement": "manual_only",
        }
    
    @classmethod
    def get_defaults(cls, project_id: str) -> "EngineConfig":
        """Create config with all default values."""
        return cls(project_id=project_id)


# Sites above this threshold get tighter iteration/MILP caps (e.g. Bangladesh ~2k sites).
_LARGE_NETWORK_SITE_THRESHOLD = 1200

_LARGE_NETWORK_CAPS: Dict[str, int] = {
    "max_iterations": 8,
    "max_conversion_iterations": 2,
    "max_capacity_ladder_rounds": 3,
    "milp_max_subgraph_size": 150,
    "scored_expansion_candidates": 5,
}


def cap_config_for_large_network(
    config: EngineConfig,
    lab_count: int,
    facility_count: int,
) -> bool:
    """
    Lower iteration and MILP limits on very large networks to cut wall time.
    Preserves values already below each cap. Returns True if anything changed.
    """
    import logging

    total_sites = lab_count + facility_count
    if total_sites <= _LARGE_NETWORK_SITE_THRESHOLD:
        return False

    changes: List[str] = []
    for field, cap in _LARGE_NETWORK_CAPS.items():
        current = getattr(config, field, None)
        if current is None or current <= cap:
            continue
        changes.append(f"{field} {current}→{cap}")
        setattr(config, field, cap)

    if changes:
        logging.getLogger(__name__).info(
            "Large network caps (%d labs + %d facilities = %d sites): %s",
            lab_count,
            facility_count,
            total_sites,
            ", ".join(changes),
        )
    return bool(changes)


@dataclass
class ProjectConstraints:
    """
    Project-level constraints from dno_project_constraints table.
    """
    
    project_id: str
    
    # Boundary constraints
    enforce_district_boundary: bool = True
    allow_cross_block_referrals: bool = False
    allow_cross_taluka_referrals: bool = False
    allow_cross_state_referrals: bool = False
    
    # Budget constraints
    max_capex_budget: Optional[Decimal] = None
    max_opex_budget_annual: Optional[Decimal] = None
    max_hr_additions: Optional[int] = None
    
    # Sector preferences
    prioritize_public_sector: bool = True
    allow_private_facilities: bool = True
    
    # Transport constraints
    max_transport_distance_km: Optional[int] = None
    max_transport_time_hours: Optional[int] = None
    max_relocation_distance_km: Optional[float] = 150.0
    """Maximum distance (km) for device relocations. Default: 150km."""
    
    # Custom constraints (flexible JSON)
    custom_constraints: Dict[str, Any] = field(default_factory=dict)
