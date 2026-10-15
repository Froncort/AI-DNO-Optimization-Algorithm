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

"""Merge device catalogue JSON into NetworkState."""

from typing import Any, Dict, Mapping, Tuple


def merge_catalogue_into_state(state, catalogue: Mapping[str, Any]) -> None:
    device_types_info: Dict[str, Any] = {}
    params_by_type: Dict[Tuple[str, str], dict] = {}
    for dt in catalogue.get("device_types", []):
        device_types_info[dt["name"]] = dt
        for tp in dt.get("test_parameters", []):
            params_by_type[(dt["id"], tp["test_type_id"])] = tp
    state.device_types_info = device_types_info or None
    state.device_test_parameters_by_type = params_by_type or None
    if "mad_device_caps" in catalogue:
        state.scenario_mad_device_caps = catalogue["mad_device_caps"]
