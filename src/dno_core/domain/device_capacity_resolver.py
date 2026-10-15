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
Live annual testing capacity from catalogue params (single formula).

All/geo capacity uses MAX(max_tests_per_shift) per device type.
Cost/CPT uses MIN via db_loader._pick_canonical_test_param — not here.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union


def annual_capacity_from_params(
    max_tests_per_shift: int,
    max_shifts: int,
    working_days: int,
    *,
    stored_fallback: int = 0,
) -> int:
    """mps × shifts × working_days; legacy stored only when live params are zero."""
    live = (
        int(max_tests_per_shift or 0)
        * max(1, int(max_shifts or 1))
        * int(working_days or 300)
    )
    if live > 0:
        return live
    return int(stored_fallback or 0)


def _max_mps_from_test_param_rows(rows: Iterable[dict]) -> int:
    """MAX(mps) across test parameter dicts."""
    vals = [int(r.get("max_tests_per_shift") or 0) for r in rows]
    return max(vals) if vals else 0


def catalog_mps_for_device_type(
    device_type_id: Optional[str],
    capacity_params: Optional[Dict[str, int]],
) -> int:
    """Baseline load: precomputed MAX(mps) per device_type_id from db_loader."""
    if not device_type_id or not capacity_params:
        return 0
    return int(capacity_params.get(str(device_type_id), 0) or 0)


def catalog_mps_from_device_type_record(device_type: Optional[dict]) -> int:
    """MAX(mps) from one effective device type dict (scenario catalogue row)."""
    if not device_type:
        return 0
    params = device_type.get("test_parameters") or []
    return _max_mps_from_test_param_rows(params)


def catalog_shifts_and_mps_from_device_type_record(rec: dict) -> tuple[int, int]:
    """Shifts + mps from the MAX-mps test row (capacity plane)."""
    params = rec.get("test_parameters") or []
    if not params:
        return 1, 0
    best = max(params, key=lambda r: int(r.get("max_tests_per_shift") or 0))
    return max(1, int(best.get("max_shifts") or 1)), int(best.get("max_tests_per_shift") or 0)


def catalog_mps_from_types_info(
    device_type_id: str,
    device_types: Union[List[dict], Dict[str, dict], None],
) -> int:
    """
    Resolve MAX(mps) for a device type id from catalogue list or name-keyed dict.
    """
    if not device_type_id or not device_types:
        return 0
    tid = str(device_type_id)
    if isinstance(device_types, dict):
        for dt in device_types.values():
            if str(dt.get("id") or "") == tid:
                return catalog_mps_from_device_type_record(dt)
        return 0
    for dt in device_types:
        if str(dt.get("id") or "") == tid:
            return catalog_mps_from_device_type_record(dt)
    return 0


def refresh_device_mps_from_catalog(
    devices: Dict[str, Any],
    device_types: Union[List[dict], Dict[str, dict], None],
) -> None:
    """Set max_tests_per_shift on every device from effective catalogue MAX(mps)."""
    if not devices or not device_types:
        return
    # Build id → mps once
    id_to_mps: Dict[str, int] = {}
    rows: Iterable[dict]
    if isinstance(device_types, dict):
        rows = device_types.values()
    else:
        rows = device_types
    for dt in rows:
        dtid = str(dt.get("id") or "")
        if not dtid:
            continue
        mps = catalog_mps_from_device_type_record(dt)
        if mps > 0:
            id_to_mps[dtid] = mps
    for dev in devices.values():
        dtid = str(getattr(dev, "device_type_id", "") or "")
        mps = id_to_mps.get(dtid, 0)
        if mps > 0:
            dev.max_tests_per_shift = mps
