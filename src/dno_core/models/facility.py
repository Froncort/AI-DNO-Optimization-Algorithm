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
Facility model for health facilities and diagnostic labs.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any, List
from datetime import datetime


@dataclass
class Facility:
    """
    Health facility or diagnostic lab.
    
    Attributes:
        id: Unique facility identifier (UUID)
        project_id: Project this facility belongs to
        external_id: External system ID
        name: Facility name
        facility_tier: Tier (PHC, CHC, SDH, DH, Medical College, etc.)
        infrastructure_level: Infrastructure level (Level 1-5)
        sector_type: PUBLIC or PRIVATE
        admin_area_1: State/Province (larger admin boundary)
        admin_area_2: District/Block (smaller admin boundary)
        latitude: Geographic latitude
        longitude: Geographic longitude
        is_lab: Whether this facility has diagnostic devices
        is_active: Whether facility is currently active
        hr_technician_count: Number of technicians
        operational_shifts: Number of shifts per day
    """
    
    # Identity
    id: str
    project_id: str
    name: str
    external_id: Optional[str] = None
    
    # Classification
    facility_tier: Optional[str] = None
    infrastructure_level: Optional[str] = None
    sector_type: str = "PUBLIC"
    
    # Location
    state: Optional[str] = None
    district: Optional[str] = None
    admin_area_1: Optional[str] = None
    admin_area_2: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    address: Optional[str] = None
    
    # Operational status
    is_lab: bool = False
    # HF | HUB | LAB — used by scenario patches / geography (DB row has no single column)
    facility_type: Optional[str] = None
    is_hub: bool = False
    is_active: bool = True
    operational_status: str = "FUNCTIONAL"
    
    # HR
    hr_technician_count: int = 0
    operational_shifts: int = 1
    
    # Population (for accessibility weighting)
    population: int = 0  # Population served by this facility (from WorldPop or census)
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    
    @property
    def has_coordinates(self) -> bool:
        """Check if facility has valid coordinates."""
        return self.latitude is not None and self.longitude is not None
    
    @property
    def tier_level(self) -> int:
        """Numeric tier position 1–5 (higher = more capable); 0 if unknown."""
        from dno_core.domain.infrastructure_tier import canonical_tier_position, effective_tier_raw

        return canonical_tier_position(effective_tier_raw(self))

    @property
    def infrastructure_score(self) -> int:
        """Numeric tier position 1–5 from canonical tier resolution; 0 if unknown."""
        from dno_core.domain.infrastructure_tier import canonical_tier_position, effective_tier_raw

        return canonical_tier_position(effective_tier_raw(self))
    
    def is_in_same_admin_area(
        self, 
        other: "Facility", 
        level: str = "admin_area_2"
    ) -> bool:
        """
        Check if another facility is in the same admin area.
        
        Args:
            other: Another facility to compare
            level: Which admin level to check ("admin_area_1" or "admin_area_2")
            
        Returns:
            True if in same admin area
        """
        if level == "admin_area_1":
            return self.admin_area_1 == other.admin_area_1
        return self.admin_area_2 == other.admin_area_2
    
    def can_host_device(
        self, 
        device_type: str, 
        tier_requirements: Dict[str, int],
        infrastructure_requirements: Optional[Dict[str, List[str]]] = None
    ) -> bool:
        """
        Check if facility can host a specific device type.
        
        Args:
            device_type: Device type to check
            tier_requirements: Dict mapping device types to required tier levels
            infrastructure_requirements: Optional infrastructure needs per device
            
        Returns:
            True if device can operate at this facility
        """
        required_tier = tier_requirements.get(device_type, 0)
        if self.tier_level < required_tier:
            return False
        
        # Additional infrastructure checks could go here
        if infrastructure_requirements:
            required_infra = infrastructure_requirements.get(device_type, [])
            # Check if facility has required infrastructure
            # (Would need facility infrastructure data to fully implement)
        
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "external_id": self.external_id,
            "name": self.name,
            "facility_tier": self.facility_tier,
            "infrastructure_level": self.infrastructure_level,
            "sector_type": self.sector_type,
            "state": self.state,
            "district": self.district,
            "admin_area_1": self.admin_area_1,
            "admin_area_2": self.admin_area_2,
            "latitude": float(self.latitude) if self.latitude else None,
            "longitude": float(self.longitude) if self.longitude else None,
            "is_lab": self.is_lab,
            "is_active": self.is_active,
            "hr_technician_count": self.hr_technician_count,
            "operational_shifts": self.operational_shifts,
            "tier_level": self.tier_level,
            "infrastructure_score": self.infrastructure_score,
        }


@dataclass
class FacilityEligibility:
    """
    Result of facility-to-lab conversion eligibility check.
    """
    
    facility_id: str
    facility_name: str
    is_eligible: bool
    confidence_score: float  # 0.0 to 1.0
    
    # Compatibility
    compatible_device_types: List[str] = field(default_factory=list)
    recommended_device_type: Optional[str] = None
    
    # Gaps identified
    infrastructure_gaps: List[str] = field(default_factory=list)
    hr_gaps: List[str] = field(default_factory=list)
    
    # Estimated costs
    estimated_conversion_cost: Decimal = Decimal("0")
    estimated_annual_operational_cost: Decimal = Decimal("0")
    
    # Impact
    population_served: int = 0
    facilities_gaining_access: List[str] = field(default_factory=list)
    
    # Reasoning
    eligibility_reason: str = ""
    ineligibility_reason: Optional[str] = None
