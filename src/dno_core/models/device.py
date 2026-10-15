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
Device inventory model with atomic device support.

Core principle: Every device has quantity=1 (atomic modeling).
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DeviceInventory:
    """
    Atomic device model - quantity always 1.
    
    When loading from DB where quantity > 1, the loader expands
    into multiple atomic instances with unique device_ids.
    
    Example:
        DB row: lab_id=Lab001, device_type=GX4, quantity=3
        Expands to:
        - DeviceInventory(device_id="GX4_LAB001_001", quantity=1, ...)
        - DeviceInventory(device_id="GX4_LAB001_002", quantity=1, ...)
        - DeviceInventory(device_id="GX4_LAB001_003", quantity=1, ...)
    
    Attributes:
        facility_id: ID of the facility/lab where device is located
        device_type: Device type name (e.g., "GX 4", "GX 16", "Tr Duo")
        device_id: Unique identifier for this atomic device
        device_type_id: Reference to ref_device_types table
        quantity: Always 1 for atomic devices
        max_shifts: Maximum shifts this device can operate per day
        max_tests_per_shift: Maximum tests per shift
        stored_annual_capacity: Ingest snapshot only; annual_capacity() uses live
            mps × shifts × days and falls back here when catalogue mps is missing.
        modules: Number of modules/cartridge slots
        cost_per_test: Cost per test conducted
        overhead_cost_annual: Device overhead + warranty (yearly)
        shift_cost_annual: Yearly HR cost per shift
        startup_cost: One-time device setup cost
        current_utilization: Current utilization ratio (0.0-1.0)
        status: Device operational status
    """
    
    # Identity
    facility_id: str
    device_type: str
    device_id: str
    device_type_id: Optional[str] = None
    
    # Atomic enforcement
    quantity: int = 1  # ALWAYS 1
    
    # Capacity parameters
    max_shifts: int = 1
    max_tests_per_shift: int = 0
    stored_annual_capacity: int = 0
    modules: int = 1
    
    # Cost parameters
    cost_per_test: Decimal = Decimal("0")
    overhead_cost_annual: Decimal = Decimal("0")
    shift_cost_annual: Decimal = Decimal("0")
    startup_cost: Decimal = Decimal("0")
    
    # Utilization
    current_utilization: float = 0.0
    tests_conducted_annual: int = 0
    
    # Status
    status: str = "OPERATIONAL"
    last_calibrated_at: Optional[datetime] = None
    
    # Whether this device was newly added in the current scenario (drives startup cost inclusion)
    is_new: bool = False
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Enforce atomic device invariant."""
        if self.quantity != 1:
            raise ValueError(
                f"DeviceInventory must have quantity=1 (atomic). "
                f"Got quantity={self.quantity} for {self.device_id}"
            )
    
    def annual_capacity(self, working_days: int = 300) -> int:
        """
        Live capacity: max_tests_per_shift × max_shifts × working_days.

        ``stored_annual_capacity`` is legacy fallback only when catalogue mps is zero.
        """
        from dno_core.domain.device_capacity_resolver import annual_capacity_from_params

        cap = annual_capacity_from_params(
            self.max_tests_per_shift,
            self.max_shifts,
            working_days,
            stored_fallback=self.stored_annual_capacity,
        )
        if cap <= 0:
            logger.warning(
                "Device %s (%s) has zero annual capacity: max_tests_per_shift=%s; "
                "check ref_device_test_parameters.",
                self.device_id,
                self.device_type,
                self.max_tests_per_shift,
            )
        return cap
    
    def operational_cost_annual(self) -> Decimal:
        """
        Calculate full annual operational cost.
        
        Formula: (shift_cost_annual × max_shifts) + overhead_cost_annual
        
        Returns:
            Total annual operational cost
        """
        return (self.shift_cost_annual * self.max_shifts) + self.overhead_cost_annual
    
    def annualized_startup_cost(self, annualization_years: int = 5) -> Decimal:
        """
        Calculate annualized startup cost.
        
        Formula: startup_cost / annualization_years
        
        Args:
            annualization_years: Period over which to spread startup cost
            
        Returns:
            Annualized startup cost
        """
        if annualization_years <= 0:
            return Decimal("0")
        return self.startup_cost / annualization_years
    
    def total_annual_cost(
        self, 
        annualization_years: int = 5,
        include_startup: bool = True
    ) -> Decimal:
        """
        Calculate total annual cost including operational + annualized startup.
        
        Args:
            annualization_years: Period for startup cost annualization
            include_startup: Whether to include annualized startup cost
            
        Returns:
            Total annual cost
        """
        total = self.operational_cost_annual()
        if include_startup:
            total += self.annualized_startup_cost(annualization_years)
        return total
    
    def is_underutilized(self, threshold: float = 0.40) -> bool:
        """Check if device is underutilized."""
        return self.current_utilization < threshold
    
    def is_overutilized(self, threshold: float = 0.95) -> bool:
        """Check if device is overutilized."""
        return self.current_utilization > threshold
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "facility_id": self.facility_id,
            "device_type": self.device_type,
            "device_id": self.device_id,
            "device_type_id": self.device_type_id,
            "quantity": self.quantity,
            "max_shifts": self.max_shifts,
            "max_tests_per_shift": self.max_tests_per_shift,
            "stored_annual_capacity": self.stored_annual_capacity,
            "modules": self.modules,
            "cost_per_test": float(self.cost_per_test),
            "overhead_cost_annual": float(self.overhead_cost_annual),
            "shift_cost_annual": float(self.shift_cost_annual),
            "startup_cost": float(self.startup_cost),
            "current_utilization": self.current_utilization,
            "tests_conducted_annual": self.tests_conducted_annual,
            "status": self.status,
            "annual_capacity": self.annual_capacity(),
            "operational_cost_annual": float(self.operational_cost_annual()),
        }
