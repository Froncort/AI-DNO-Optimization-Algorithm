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

"""Public goal surface — maps presets to DynamicScenarioConfig without editing goals.py."""

from dataclasses import dataclass
from typing import Literal

from dno_core.domain.labels import (
    FocusName,
    PresetName,
    SCENARIO_SPECS,
    default_focus,
    objective_weights_for,
)
from dno_core.models.goals import DynamicScenarioConfig


@dataclass
class OssGoal:
    preset: PresetName
    focus: FocusName = "balanced"
    focus_explicit: bool = False
    partition_by_admin_area: bool = False
    admin_area_level: Literal["admin_area_1", "admin_area_2"] = "admin_area_1"

    def __post_init__(self) -> None:
        if self.preset not in SCENARIO_SPECS:
            raise ValueError(
                f"Unknown OssGoal preset {self.preset!r}; "
                f"must be one of {sorted(SCENARIO_SPECS)}"
            )
        if not self.focus_explicit:
            self.focus = default_focus(self.preset)

    def to_scenario_config(self) -> DynamicScenarioConfig:
        spec = SCENARIO_SPECS[self.preset]
        return DynamicScenarioConfig(
            optimization_modes=list(spec.modes),
            expansion_levers=dict(spec.expansion_levers),
            allow_conversions=spec.allow_conversions,
            objective_weights=objective_weights_for(
                self.preset, self.focus, focus_explicit=self.focus_explicit
            ),
            optimize_by_admin_area=self.partition_by_admin_area,
            optimize_admin_area_level=self.admin_area_level,
        )
