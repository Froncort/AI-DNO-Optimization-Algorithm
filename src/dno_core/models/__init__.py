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

"""Data models for DNO Engine."""

from dno_core.models.device import DeviceInventory
from dno_core.models.facility import Facility
from dno_core.models.referral import Referral, SyntheticReferral
from dno_core.models.actions import (
    RelocationAction,
    FacilityConversionAction,
    ExpansionAction,
    RedesignAction,
)
from dno_core.models.goals import (
    OptimizationGoal,
    CapacityGoal,
    DemandProjectionGoal,
    CoverageGoal,
    UtilizationBalanceGoal,
)
from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.results import (
    OptimizationResult,
    Matrix3x3Result,
    FinancialAxisScore,
    ProgrammaticAxisScore,
    ImpactAxisScore,
    UtilizationReport,
)

__all__ = [
    # Device
    "DeviceInventory",
    # Facility
    "Facility",
    # Referral
    "Referral",
    "SyntheticReferral",
    # Actions
    "RelocationAction",
    "FacilityConversionAction",
    "ExpansionAction",
    "RedesignAction",
    # Goals
    "OptimizationGoal",
    "CapacityGoal",
    "DemandProjectionGoal",
    "CoverageGoal",
    "UtilizationBalanceGoal",
    # Config
    "EngineConfig",
    # State
    "NetworkState",
    # Results
    "OptimizationResult",
    "Matrix3x3Result",
    "FinancialAxisScore",
    "ProgrammaticAxisScore",
    "ImpactAxisScore",
    "UtilizationReport",
]
