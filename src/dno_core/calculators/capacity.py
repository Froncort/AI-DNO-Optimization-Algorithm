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
Capacity calculator for device and network capacity.
"""

from typing import Dict, List
import logging

from dno_core.models.device import DeviceInventory
from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState

logger = logging.getLogger(__name__)


class CapacityCalculator:
    """
    Calculate device and network capacity.
    """
    
    def __init__(self, config: EngineConfig):
        """
        Initialize capacity calculator.
        
        Args:
            config: Engine configuration
        """
        self.config = config
        self.working_days = config.working_days_per_year
        self.default_shifts = config.default_shifts_per_day
    
    def device_capacity(self, device: DeviceInventory) -> int:
        """
        Calculate annual capacity for a single device.
        
        Formula: max_tests_per_shift × max_shifts × working_days
        """
        return device.annual_capacity(self.working_days)
    
    def lab_capacity(
        self, 
        lab_id: str, 
        devices: Dict[str, DeviceInventory]
    ) -> int:
        """
        Calculate total annual capacity at a lab.
        """
        return sum(
            self.device_capacity(d)
            for d in devices.values()
            if d.facility_id == lab_id
        )
    
    def network_capacity(self, state: NetworkState) -> int:
        """
        Calculate total annual capacity across entire network.
        """
        return sum(
            self.device_capacity(d)
            for d in state.devices.values()
        )
    
    def capacity_by_lab(self, state: NetworkState) -> Dict[str, int]:
        """
        Calculate capacity for each lab.
        
        Returns dict mapping lab_id → annual capacity
        """
        capacities: Dict[str, int] = {}
        
        for device in state.devices.values():
            lab_id = device.facility_id
            capacities[lab_id] = capacities.get(lab_id, 0) + self.device_capacity(device)
        
        return capacities
    
    def capacity_by_device_type(self, state: NetworkState) -> Dict[str, int]:
        """
        Calculate total capacity by device type.
        
        Returns dict mapping device_type → total annual capacity
        """
        capacities: Dict[str, int] = {}
        
        for device in state.devices.values():
            dt = device.device_type
            capacities[dt] = capacities.get(dt, 0) + self.device_capacity(device)
        
        return capacities
    
    def capacity_gap(
        self, 
        state: NetworkState, 
        target_capacity: int
    ) -> int:
        """
        Calculate capacity gap to meet target.
        
        Returns positive number if more capacity needed, 0 if sufficient.
        """
        current = self.network_capacity(state)
        return max(0, target_capacity - current)
    
    def devices_needed_for_capacity(
        self,
        capacity_gap: int,
        device_type: str,
        device_types_info: Dict[str, Dict]
    ) -> int:
        """
        Calculate number of devices needed to fill capacity gap.
        
        Args:
            capacity_gap: Capacity shortfall to fill
            device_type: Device type to use
            device_types_info: Device type reference data
            
        Returns:
            Number of devices needed (rounded up)
        """
        if capacity_gap <= 0:
            return 0
        
        dt_info = device_types_info.get(device_type, {})
        samples_per_cycle = dt_info.get("samples_per_cycle", 1)
        cycle_time = dt_info.get("cycle_time_minutes", 60)
        
        # Calculate capacity per device
        cycles_per_shift = (8 * 60) // cycle_time if cycle_time > 0 else 1
        tests_per_shift = samples_per_cycle * cycles_per_shift
        capacity_per_device = tests_per_shift * self.default_shifts * self.working_days
        
        if capacity_per_device <= 0:
            return 0
        
        # Round up
        devices_needed = (capacity_gap + capacity_per_device - 1) // capacity_per_device
        return devices_needed
    
    def available_capacity_at_lab(
        self,
        lab_id: str,
        state: NetworkState
    ) -> int:
        """
        Calculate available (unused) capacity at a lab.
        
        Formula: capacity - current_demand
        """
        capacity = self.lab_capacity(lab_id, state.devices)
        
        # Sum demand from referrals to this lab
        demand = sum(
            state.test_volumes.get(r.facility_id, 0)
            for r in state.referrals
            if r.lab_id == lab_id
        )
        
        return max(0, capacity - demand)
