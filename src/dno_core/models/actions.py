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
Action models for optimization operations.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any


@dataclass
class RelocationAction:
    """
    Device relocation between labs.
    
    Represents moving a single atomic device from one lab to another.
    May also represent moving a device to a newly converted facility.
    
    Attributes:
        source_lab_id: Lab device is being moved from
        target_lab_id: Lab device is being moved to
        device_type: Type of device being moved
        device_id: Specific atomic device ID
        device_capacity_annual: Annual test capacity of the device
        transport_cost: Cost to physically move the device
        setup_cost: Cost to install at new location
        rationale: Human-readable explanation
        selection_score: Score that determined this device selection
        target_is_new_lab: True if target is a converted facility
        conversion_details: Details if target is being converted
        policy_applied: Which conversion policy was used
    """
    
    source_lab_id: str
    target_lab_id: str
    device_type: str
    device_id: str
    
    # Names for display
    source_lab_name: str = ""
    target_lab_name: str = ""
    
    # Capacity
    device_capacity_annual: int = 0
    
    # Costs
    transport_cost: Decimal = Decimal("0")
    setup_cost: Decimal = Decimal("0")
    
    # Reasoning
    rationale: str = ""
    selection_score: float = 0.0
    
    # Conversion context
    target_is_new_lab: bool = False
    conversion_details: Optional[Dict[str, Any]] = None
    policy_applied: str = "lab_to_lab_first"
    
    @property
    def total_relocation_cost(self) -> Decimal:
        """Total one-time cost for this relocation."""
        return self.transport_cost + self.setup_cost
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "RELOCATION",
            "source_lab_id": self.source_lab_id,
            "target_lab_id": self.target_lab_id,
            "source_lab_name": self.source_lab_name,
            "target_lab_name": self.target_lab_name,
            "device_type": self.device_type,
            "device_id": self.device_id,
            "device_capacity_annual": self.device_capacity_annual,
            "transport_cost": float(self.transport_cost),
            "setup_cost": float(self.setup_cost),
            "total_cost": float(self.total_relocation_cost),
            "rationale": self.rationale,
            "target_is_new_lab": self.target_is_new_lab,
            "policy_applied": self.policy_applied,
        }


@dataclass
class FacilityConversionAction:
    """
    Facility-to-lab conversion.
    
    Represents upgrading a health facility to become a diagnostic lab
    by installing equipment and necessary infrastructure.
    
    Attributes:
        facility_id: Facility being converted
        facility_name: Human-readable name
        recommended_device_type: Device type to install
        estimated_conversion_cost: Total one-time conversion cost
        estimated_annual_operational_cost: Yearly running cost
        confidence_score: Eligibility confidence (0-1)
        compatible_devices: All devices that can be installed
        population_served: Population that gains access
        facilities_gaining_access: Nearby facilities that benefit
        rationale: Human-readable explanation
    """
    
    facility_id: str
    facility_name: str
    
    # Device recommendation
    recommended_device_type: str = ""
    compatible_devices: List[str] = field(default_factory=list)
    
    # Costs
    estimated_conversion_cost: Decimal = Decimal("0")
    estimated_annual_operational_cost: Decimal = Decimal("0")
    
    # Confidence
    confidence_score: float = 0.0
    
    # Impact
    population_served: int = 0
    facilities_gaining_access: List[str] = field(default_factory=list)
    
    # Reasoning
    rationale: str = ""
    policy_preference: str = "conversion_allowed"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "CONVERSION",
            "facility_id": self.facility_id,
            "facility_name": self.facility_name,
            "recommended_device_type": self.recommended_device_type,
            "compatible_devices": self.compatible_devices,
            "estimated_conversion_cost": float(self.estimated_conversion_cost),
            "estimated_annual_operational_cost": float(self.estimated_annual_operational_cost),
            "confidence_score": self.confidence_score,
            "population_served": self.population_served,
            "facilities_gaining_access": self.facilities_gaining_access,
            "rationale": self.rationale,
        }


@dataclass
class ExpansionAction:
    """
    Goal-driven device addition.
    
    Represents adding new devices to existing labs to meet capacity goals.
    
    Attributes:
        lab_id: Lab receiving new device
        device_type: Type of device to add
        quantity: Number of devices (typically 1 for atomic)
        total_startup_cost: One-time cost
        annualized_startup_cost: Startup cost spread over years
        annual_operational_cost: Yearly running cost
        goal_contribution: How this helps meet the goal
        capacity_added_annual: Annual test capacity added
    """
    
    lab_id: str
    device_type: str
    quantity: int = 1  # Atomic devices
    
    # Catalogue identity for multi-test capacity
    device_type_id: Optional[str] = None
    activated_test_type_ids: List[str] = field(default_factory=list)
    
    # Lab info for display
    lab_name: str = ""
    
    # Costs
    total_startup_cost: Decimal = Decimal("0")
    annualized_startup_cost: Decimal = Decimal("0")
    annual_operational_cost: Decimal = Decimal("0")
    
    # Goal alignment
    goal_contribution: str = ""
    capacity_added_annual: int = 0
    
    # Reasoning
    rationale: str = ""
    expansion_score: float = 0.0
    redesign_validated: bool = False
    """True when Phase 3.5 scored this expansion after a hypothetical MILP redesign."""

    # True when lab_id was a plain facility before this action (facility→lab conversion).
    # The scenario_state_loader uses this to promote the facility to a lab before
    # creating the device so the site appears on the map as a lab.
    is_new_lab: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "EXPANSION",
            "lab_id": self.lab_id,
            "lab_name": self.lab_name,
            "device_type": self.device_type,
            "device_type_id": self.device_type_id,
            "activated_test_type_ids": list(self.activated_test_type_ids or []),
            "quantity": self.quantity,
            "total_startup_cost": float(self.total_startup_cost),
            "annualized_startup_cost": float(self.annualized_startup_cost),
            "annual_operational_cost": float(self.annual_operational_cost),
            "capacity_added_annual": self.capacity_added_annual,
            "goal_contribution": self.goal_contribution,
            "rationale": self.rationale,
            "redesign_validated": self.redesign_validated,
            "is_new_lab": self.is_new_lab,
        }


@dataclass
class RedesignAction:
    """
    Referral network redesign action.
    
    Represents changing which lab a facility refers samples to.
    
    Attributes:
        facility_id: Facility whose referral is being changed
        old_lab_id: Previous lab assignment
        new_lab_id: New lab assignment
        distance_change_km: Change in referral distance
        new_distance_km: Geodesic km facility→new lab (no circuity); persisted on route patches
        cost_change: Change in transport cost
        rationale: Human-readable explanation
    """
    
    facility_id: str
    old_lab_id: str
    new_lab_id: str
    
    # Names for display
    facility_name: str = ""
    old_lab_name: str = ""
    new_lab_name: str = ""
    
    distance_change_km: float = 0.0
    """Delta using effective (×1.4) distances — matches MILP objective."""
    new_distance_km: float = 0.0
    """Raw Haversine km to new lab; written to scenario patch as ``distance_km``."""
    cost_change: Decimal = Decimal("0")
    
    # Admin boundary info
    crosses_admin_boundary: bool = False
    admin_boundary_level: Optional[str] = None
    
    rationale: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "REDESIGN",
            "facility_id": self.facility_id,
            "old_lab_id": self.old_lab_id,
            "new_lab_id": self.new_lab_id,
            "facility_name": self.facility_name,
            "old_lab_name": self.old_lab_name,
            "new_lab_name": self.new_lab_name,
            "distance_change_km": self.distance_change_km,
            "distance_km": self.new_distance_km,
            "cost_change": float(self.cost_change),
            "crosses_admin_boundary": self.crosses_admin_boundary,
            "rationale": self.rationale,
        }


@dataclass
class TestIntegrationAction:
    """
    Activate an overflow test on a co-located multi-test device (same lab, capex 0).
    """

    lab_id: str
    device_id: str
    device_type: str
    test_type_id: str
    lab_name: str = ""
    device_type_name: str = ""
    test_type_name: str = ""
    shift_units_used: float = 0.0
    capacity_equivalent_tests: int = 0
    total_startup_cost: Decimal = Decimal("0")
    annual_operational_cost: Decimal = Decimal("0")
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "TEST_INTEGRATION",
            "lab_id": self.lab_id,
            "lab_name": self.lab_name,
            "device_id": self.device_id,
            "device_type": self.device_type,
            "device_type_name": self.device_type_name,
            "activated_test_type_id": self.test_type_id,
            "test_type_id": self.test_type_id,
            "test_type_name": self.test_type_name,
            "shift_units_used": self.shift_units_used,
            "capacity_equivalent_tests": self.capacity_equivalent_tests,
            "total_startup_cost": float(self.total_startup_cost),
            "annual_operational_cost": float(self.annual_operational_cost),
            "rationale": self.rationale,
        }


@dataclass
class AddShiftAction:
    """Raise operational shifts on an existing device (opex only)."""

    lab_id: str
    device_id: str
    device_type: str
    old_operational_shifts: int
    operational_shifts: int
    lab_name: str = ""
    device_type_name: str = ""
    total_startup_cost: Decimal = Decimal("0")
    annual_operational_cost: Decimal = Decimal("0")
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": "ADD_SHIFT",
            "lab_id": self.lab_id,
            "lab_name": self.lab_name,
            "device_id": self.device_id,
            "device_type": self.device_type,
            "device_type_name": self.device_type_name,
            "old_operational_shifts": self.old_operational_shifts,
            "operational_shifts": self.operational_shifts,
            "total_startup_cost": float(self.total_startup_cost),
            "annual_operational_cost": float(self.annual_operational_cost),
            "rationale": self.rationale,
        }


# Type alias for any action
Action = (
    RelocationAction
    | FacilityConversionAction
    | ExpansionAction
    | RedesignAction
    | TestIntegrationAction
    | AddShiftAction
)
