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

"""MAD (Maximum Allowable Devices) JSON normalization, merge, and resolution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from dno_core.domain.infrastructure_tier import effective_tier_raw, resolve_tier_key


@dataclass(frozen=True)
class DeviceLimitResult:
    limit: int
    source: str
    tier_key: str = ""


def default_mad_caps() -> dict:
    return {
        "version": 1,
        "global_max_devices": None,
        "admin_overrides": [],
        "facility_overrides": [],
        "test_overrides": [],
    }


def admin_key(value: Any) -> Optional[str]:
    cleaned = (value or "").strip()
    return cleaned.casefold() if cleaned else None


def normalize_mad_device_caps(raw: Any) -> dict:
    if not raw or not isinstance(raw, dict):
        return default_mad_caps()

    g = raw.get("global_max_devices")
    global_max = None
    if g is not None:
        try:
            global_max = int(g)
        except (TypeError, ValueError):
            global_max = None

    admin_overrides: List[dict] = []
    for row in raw.get("admin_overrides") or []:
        if not isinstance(row, dict):
            continue
        a1 = (row.get("admin_area_1") or "").strip()
        if not a1:
            continue
        try:
            max_devices = int(row["max_devices"])
        except (KeyError, TypeError, ValueError):
            continue
        admin_overrides.append(
            {
                "admin_area_1": a1,
                "admin_area_2": (row.get("admin_area_2") or "").strip() or None,
                "max_devices": max_devices,
            }
        )

    facility_overrides: List[dict] = []
    for row in raw.get("facility_overrides") or []:
        if not isinstance(row, dict):
            continue
        fid = (row.get("facility_id") or "").strip()
        if not fid:
            continue
        try:
            max_devices = int(row["max_devices"])
        except (KeyError, TypeError, ValueError):
            continue
        facility_overrides.append({"facility_id": fid, "max_devices": max_devices})

    test_overrides: List[dict] = []
    for row in raw.get("test_overrides") or []:
        if not isinstance(row, dict):
            continue
        fid = (row.get("facility_id") or "").strip()
        tid = (row.get("test_type_id") or "").strip()
        if not fid or not tid:
            continue
        try:
            max_devices = int(row["max_devices"])
        except (KeyError, TypeError, ValueError):
            continue
        test_overrides.append(
            {"facility_id": fid, "test_type_id": tid, "max_devices": max_devices}
        )

    return {
        "version": 1,
        "global_max_devices": global_max,
        "admin_overrides": admin_overrides,
        "facility_overrides": facility_overrides,
        "test_overrides": test_overrides,
    }


def extract_project_mad(config) -> dict:
    sr = getattr(config, "situational_rules", None) or {}
    return normalize_mad_device_caps(sr.get("mad_device_caps"))


def extract_scenario_mad(optimization_config: Any) -> dict:
    from dno_core.domain.json_config import _parse_json_config

    cfg = _parse_json_config(optimization_config)
    return normalize_mad_device_caps(cfg.get("mad_device_caps"))


def merge_mad_caps(project_caps: dict, scenario_caps: Optional[dict]) -> dict:
    project = normalize_mad_device_caps(project_caps)
    if not scenario_caps:
        return project
    scenario = normalize_mad_device_caps(scenario_caps)

    merged = dict(project)
    if scenario.get("global_max_devices") is not None:
        merged["global_max_devices"] = scenario["global_max_devices"]

    def _merge_rows(key: str, id_fn):
        by_id: Dict[Any, dict] = {id_fn(r): r for r in project.get(key) or []}
        for row in scenario.get(key) or []:
            by_id[id_fn(row)] = row
        merged[key] = list(by_id.values())

    _merge_rows("facility_overrides", lambda r: r["facility_id"])
    _merge_rows(
        "admin_overrides",
        lambda r: (admin_key(r.get("admin_area_1")), admin_key(r.get("admin_area_2"))),
    )
    _merge_rows(
        "test_overrides",
        lambda r: (r["facility_id"], r["test_type_id"]),
    )
    return normalize_mad_device_caps(merged)


def merge_mad_into_optimization_config(existing: Any, mad_device_caps: dict) -> dict:
    from dno_core.domain.json_config import _parse_json_config

    cfg = _parse_json_config(existing)
    cfg["mad_device_caps"] = normalize_mad_device_caps(mad_device_caps)
    return cfg


def _lookup_admin(caps: dict, lab, prefix: str) -> Optional[DeviceLimitResult]:
    k1 = admin_key(getattr(lab, "admin_area_1", None))
    k2 = admin_key(getattr(lab, "admin_area_2", None))
    best: Optional[DeviceLimitResult] = None
    for row in caps.get("admin_overrides") or []:
        ra1 = admin_key(row.get("admin_area_1"))
        ra2 = admin_key(row.get("admin_area_2"))
        if ra1 != k1:
            continue
        if ra2 and ra2 != k2:
            continue
        source = f"{prefix}_admin2" if ra2 else f"{prefix}_admin1"
        best = DeviceLimitResult(int(row["max_devices"]), source)
        if ra2:
            return best
    return best


def resolve_device_limit(lab, config, project_caps: dict, scenario_caps: Optional[dict]) -> DeviceLimitResult:
    project = normalize_mad_device_caps(project_caps)
    scenario = normalize_mad_device_caps(scenario_caps) if scenario_caps else default_mad_caps()
    fid = str(getattr(lab, "id", ""))

    for row in scenario.get("facility_overrides") or []:
        if row["facility_id"] == fid:
            return DeviceLimitResult(int(row["max_devices"]), "scenario_facility")
    for row in project.get("facility_overrides") or []:
        if row["facility_id"] == fid:
            return DeviceLimitResult(int(row["max_devices"]), "project_facility")

    hit = _lookup_admin(scenario, lab, "scenario")
    if hit:
        return hit
    hit = _lookup_admin(project, lab, "project")
    if hit:
        return hit

    if scenario.get("global_max_devices") is not None:
        return DeviceLimitResult(int(scenario["global_max_devices"]), "scenario_global")
    if project.get("global_max_devices") is not None:
        return DeviceLimitResult(int(project["global_max_devices"]), "project_global")

    tier_raw = effective_tier_raw(lab)
    key = resolve_tier_key(tier_raw, config.infra_device_limits)
    if key:
        return DeviceLimitResult(
            int(config.infra_device_limits[key]),
            "tier",
            tier_key=key,
        )
    return DeviceLimitResult(2, "fallback")


def resolve_test_type_ids_for_device_type(
    device_type_id: str, catalogue: Any
) -> Tuple[str, ...]:
    if not catalogue or not device_type_id:
        return ()
    rec = None
    if isinstance(catalogue, dict):
        rec = catalogue.get(device_type_id) or catalogue.get(str(device_type_id))
        if not rec:
            for v in catalogue.values():
                if isinstance(v, dict) and str(v.get("id")) == str(device_type_id):
                    rec = v
                    break
    if not rec or not isinstance(rec, dict):
        return ()
    ids: List[str] = []
    for tp in rec.get("test_parameters") or []:
        if isinstance(tp, dict) and tp.get("test_type_id"):
            ids.append(str(tp["test_type_id"]))
    return tuple(dict.fromkeys(ids))


def count_devices_for_test(state, facility_id: str, test_type_id: str, catalogue: Any) -> int:
    count = 0
    for dev in (getattr(state, "devices", None) or {}).values():
        if getattr(dev, "facility_id", None) != facility_id:
            continue
        dtid = getattr(dev, "device_type_id", None)
        if not dtid:
            continue
        supported = resolve_test_type_ids_for_device_type(str(dtid), catalogue)
        if test_type_id in supported:
            count += int(getattr(dev, "quantity", 1) or 1)
    return count


def validate_mad_device_caps(caps: dict, facility_ids: Optional[set] = None) -> None:
    normalized = normalize_mad_device_caps(caps)
    g = normalized.get("global_max_devices")
    if g is not None and (g < 1 or g > 500):
        raise ValueError("global_max_devices must be between 1 and 500")

    seen_facility: set = set()
    for row in normalized.get("facility_overrides") or []:
        fid = row["facility_id"]
        if fid in seen_facility:
            raise ValueError(f"Duplicate facility override: {fid}")
        seen_facility.add(fid)
        if facility_ids is not None and fid not in facility_ids:
            raise ValueError(f"Unknown facility_id: {fid}")
        if row["max_devices"] < 1:
            raise ValueError("max_devices must be >= 1")

    seen_admin: set = set()
    for row in normalized.get("admin_overrides") or []:
        scope = (admin_key(row.get("admin_area_1")), admin_key(row.get("admin_area_2")))
        if scope in seen_admin:
            raise ValueError(f"Duplicate admin override: {row.get('admin_area_1')}")
        seen_admin.add(scope)
        if row["max_devices"] < 1:
            raise ValueError("max_devices must be >= 1")

    seen_test: set = set()
    for row in normalized.get("test_overrides") or []:
        key = (row["facility_id"], row["test_type_id"])
        if key in seen_test:
            raise ValueError(f"Duplicate test override: {key}")
        seen_test.add(key)
        if facility_ids is not None and row["facility_id"] not in facility_ids:
            raise ValueError(f"Unknown facility_id: {row['facility_id']}")
        if row["max_devices"] < 1:
            raise ValueError("max_devices must be >= 1")
