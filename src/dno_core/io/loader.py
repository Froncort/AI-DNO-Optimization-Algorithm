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

"""NetworkState JSON loader — strict schema per 02_SURGERY_IO_API_CLI.md §5a."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

from dno_core.calculators.shift_capacity import seed_facility_test_capabilities
from dno_core.domain.labels import catalogue_required
from dno_core.domain.transport_referral_utils import aggregate_hf_lab_referrals
from dno_core.io.catalogue import merge_catalogue_into_state
from dno_core.models.config import EngineConfig
from dno_core.models.device import DeviceInventory
from dno_core.models.facility import Facility
from dno_core.models.network_state import NetworkState
from dno_core.models.referral import Referral, SyntheticReferral

_KNOWN_FACILITY_KEYS = {
    "id", "name", "facility_tier", "infrastructure_level", "sector_type",
    "admin_area_1", "admin_area_2", "latitude", "longitude", "is_lab",
    "is_hub", "is_active", "hr_technician_count", "operational_shifts", "population",
}
_KNOWN_DEVICE_KEYS = {
    "facility_id", "device_type", "device_id", "device_type_id", "max_shifts",
    "max_tests_per_shift", "stored_annual_capacity", "modules", "cost_per_test",
    "overhead_cost_annual", "shift_cost_annual", "startup_cost", "status", "quantity",
    "current_utilization", "tests_conducted_annual",
}
_KNOWN_REFERRAL_KEYS = {
    "facility_id", "lab_id", "referral_count", "distance_km", "is_synthetic",
    "confidence", "generation_method", "admin_boundary_crossed",
    "distance_score", "capacity_score", "admin_score", "tier_score",
}
_KNOWN_ROUTE_KEYS = {
    "id", "project_id", "origin_facility_id", "dest_facility_id", "distance_km",
    "transit_time_minutes", "transport_mode", "transport_mode_id", "cost_per_km",
    "fixed_trip_cost", "frequency_per_week", "test_type_id", "is_active", "metadata",
}
_KNOWN_TOP_LEVEL_KEYS = {
    "schema_version", "project_id", "facilities", "labs", "devices", "referrals",
    "transport_routes", "test_volumes", "test_volumes_by_type", "device_type_volumes",
    "device_type_test_volumes", "device_active_tests", "test_type_names",
    "use_historical_lab_baseline", "config", "goal_kind", "manual_capability_overrides",
}
_PRODUCTION_PRESET_MSG = "production transport presets are not supported in the JSON loader"


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal(value: Any, default: str = "0") -> Decimal:
    return _decimal_or_none(value) if value is not None else Decimal(default)


def _reject_unknown_keys(entry: Dict[str, Any], known: Set[str], context: str) -> None:
    unknown = set(entry.keys()) - known
    if unknown:
        raise ValueError(f"{context}: unknown field(s) {sorted(unknown)} — not part of the canonical schema")


def _walk_reject_production_presets(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_path = f"{path}.{key}" if path else str(key)
            if str(key).startswith("production_preset_"):
                raise ValueError(f"{key_path}: {_PRODUCTION_PRESET_MSG}")
            if key == "situational_rules" and isinstance(val, dict):
                for sk in val:
                    if str(sk).startswith("production_preset_"):
                        raise ValueError(f"{key_path}.{sk}: {_PRODUCTION_PRESET_MSG}")
            if key == "sample_transport_mode_catalog" and isinstance(val, dict):
                for ck in val:
                    if str(ck).startswith("production_preset_"):
                        raise ValueError(f"{key_path}.{ck}: {_PRODUCTION_PRESET_MSG}")
            _walk_reject_production_presets(val, key_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_reject_production_presets(item, f"{path}[{i}]")


def _read(path_or_dict: Union[str, Path, Mapping[str, Any]]) -> Dict[str, Any]:
    if isinstance(path_or_dict, Mapping):
        return dict(path_or_dict)
    text = Path(path_or_dict).read_text(encoding="utf-8")
    return json.loads(text)


def _build_facility(entry: Dict[str, Any], project_id: str, *, is_lab_default: bool) -> Facility:
    _reject_unknown_keys(entry, _KNOWN_FACILITY_KEYS, f"facility {entry.get('id')!r}")
    return Facility(
        id=entry["id"],
        project_id=project_id,
        name=entry["name"],
        facility_tier=entry.get("facility_tier"),
        infrastructure_level=entry.get("infrastructure_level"),
        sector_type=entry.get("sector_type", "PUBLIC"),
        admin_area_1=entry.get("admin_area_1"),
        admin_area_2=entry.get("admin_area_2"),
        latitude=_decimal_or_none(entry.get("latitude")),
        longitude=_decimal_or_none(entry.get("longitude")),
        is_lab=entry.get("is_lab", is_lab_default),
        is_hub=entry.get("is_hub", False),
        is_active=entry.get("is_active", True),
        hr_technician_count=entry.get("hr_technician_count", 0),
        operational_shifts=entry.get("operational_shifts", 1),
        population=entry.get("population", 0),
    )


def _build_facility_dicts(data: Dict[str, Any], project_id: str) -> Tuple[Dict[str, Facility], Dict[str, Facility]]:
    facilities: Dict[str, Facility] = {}
    labs: Dict[str, Facility] = {}
    for entry in data.get("facilities", []):
        fac = _build_facility(entry, project_id, is_lab_default=False)
        if fac.is_lab:
            labs[fac.id] = fac
        else:
            facilities[fac.id] = fac
    for entry in data.get("labs", []):
        labs[entry["id"]] = _build_facility(entry, project_id, is_lab_default=True)
    return facilities, labs


def _build_devices(entries: List[Dict[str, Any]]) -> Dict[str, DeviceInventory]:
    devices: Dict[str, DeviceInventory] = {}
    for entry in entries:
        _reject_unknown_keys(entry, _KNOWN_DEVICE_KEYS, f"device {entry.get('device_id')!r}")
        device_id = entry["device_id"]
        if device_id in devices:
            raise ValueError(f"duplicate device_id: {device_id!r} — device IDs must be unique")
        quantity = entry.get("quantity", 1)
        if quantity != 1:
            raise ValueError(
                f"device {device_id}: quantity must be 1 or omitted (atomic-device convention), got {quantity!r}"
            )
        devices[device_id] = DeviceInventory(
            facility_id=entry["facility_id"],
            device_type=entry["device_type"],
            device_id=device_id,
            device_type_id=entry.get("device_type_id"),
            max_shifts=entry.get("max_shifts", 1),
            max_tests_per_shift=entry.get("max_tests_per_shift", 0),
            stored_annual_capacity=entry.get("stored_annual_capacity", 0),
            modules=entry.get("modules", 1),
            cost_per_test=_decimal(entry.get("cost_per_test")),
            overhead_cost_annual=_decimal(entry.get("overhead_cost_annual")),
            shift_cost_annual=_decimal(entry.get("shift_cost_annual")),
            startup_cost=_decimal(entry.get("startup_cost")),
            status=entry.get("status", "OPERATIONAL"),
            current_utilization=float(entry.get("current_utilization", 0.0)),
            tests_conducted_annual=int(entry.get("tests_conducted_annual", 0)),
        )
    return devices


def _build_referrals(entries: List[Dict[str, Any]]) -> List[Any]:
    refs: List[Any] = []
    for entry in entries:
        _reject_unknown_keys(
            entry, _KNOWN_REFERRAL_KEYS, f"referral {entry.get('facility_id')!r}->{entry.get('lab_id')!r}"
        )
        if entry.get("is_synthetic"):
            refs.append(
                SyntheticReferral(
                    facility_id=entry["facility_id"],
                    lab_id=entry["lab_id"],
                    distance_km=float(entry.get("distance_km", 0.0)),
                    confidence=float(entry.get("confidence", 1.0)),
                    generation_method=entry.get("generation_method", "json_loader"),
                    admin_boundary_crossed=entry.get("admin_boundary_crossed", False),
                    distance_score=float(entry.get("distance_score", 0.0)),
                    capacity_score=float(entry.get("capacity_score", 0.0)),
                    admin_score=float(entry.get("admin_score", 0.0)),
                    tier_score=float(entry.get("tier_score", 0.0)),
                )
            )
        else:
            refs.append(
                Referral(
                    facility_id=entry["facility_id"],
                    lab_id=entry["lab_id"],
                    referral_count=entry.get("referral_count", 0),
                    distance_km=float(entry.get("distance_km", 0.0)),
                    is_synthetic=False,
                    confidence=float(entry.get("confidence", 1.0)),
                )
            )
    return refs


def _build_transport_routes(entries: List[Dict[str, Any]], project_id: str):
    from dno_core.models.referral import TransportRoute

    routes = []
    for entry in entries:
        _reject_unknown_keys(entry, _KNOWN_ROUTE_KEYS, f"transport_route {entry.get('id')!r}")
        routes.append(
            TransportRoute(
                id=entry["id"],
                project_id=entry.get("project_id", project_id),
                origin_facility_id=entry["origin_facility_id"],
                dest_facility_id=entry["dest_facility_id"],
                distance_km=float(entry.get("distance_km", 0.0)),
                transit_time_minutes=entry.get("transit_time_minutes", 0),
                transport_mode=entry.get("transport_mode", "ROAD"),
                transport_mode_id=entry.get("transport_mode_id"),
                cost_per_km=float(entry.get("cost_per_km", 0.0)),
                fixed_trip_cost=float(entry.get("fixed_trip_cost", 0.0)),
                frequency_per_week=entry.get("frequency_per_week", 1),
                test_type_id=entry.get("test_type_id"),
                is_active=entry.get("is_active", True),
                metadata=entry.get("metadata"),
            )
        )
    return routes


def _pipe_dict(raw: Optional[dict], parts: int):
    if not raw:
        return None
    out = {}
    for k, v in raw.items():
        bits = str(k).split("|")
        if len(bits) != parts:
            raise ValueError(f"malformed delimiter key {k!r} — expected {parts - 1} pipe(s)")
        out[tuple(bits)] = v
    return out


def _apply_config_overrides(config: EngineConfig, overrides: Dict[str, Any]) -> EngineConfig:
    field_names = {f.name for f in dataclasses.fields(EngineConfig)}
    for key, value in overrides.items():
        if key not in field_names:
            raise ValueError(f"config.{key} is not a recognized EngineConfig field")
        current = getattr(config, key)
        if isinstance(current, Decimal) and not isinstance(value, Decimal):
            value = Decimal(str(value))
        setattr(config, key, value)
    return config


def load_network_dict(
    data: Mapping[str, Any],
    *,
    catalogue: Optional[Mapping[str, Any]] = None,
    goal_preset: Optional[str] = None,
) -> NetworkState:
    """Strict in-memory loader used by parity harness and api."""
    payload = dict(data)
    _reject_unknown_keys(payload, _KNOWN_TOP_LEVEL_KEYS, "network JSON (top level)")
    _walk_reject_production_presets(payload)

    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema_version {payload.get('schema_version')!r}; expected 1")
    if payload.get("goal_kind") == "device_placement":
        raise ValueError("goal_kind='device_placement' is not supported — use OssGoal presets.")

    project_id = payload["project_id"]
    facilities, labs = _build_facility_dicts(payload, project_id)
    devices = _build_devices(payload.get("devices", []))
    routes = _build_transport_routes(payload.get("transport_routes", []), project_id)
    referrals = _build_referrals(payload.get("referrals", []))
    if routes and not referrals:
        referrals = aggregate_hf_lab_referrals(
            routes, facilities, labs, test_volumes=dict(payload.get("test_volumes", {}))
        )

    config = EngineConfig.get_defaults(project_id)
    if payload.get("config"):
        config = _apply_config_overrides(config, payload["config"])

    device_active = payload.get("device_active_tests")
    if device_active is not None:
        device_active = {did: set(tests) for did, tests in device_active.items()}

    state = NetworkState(
        project_id=project_id,
        facilities=facilities,
        labs=labs,
        devices=devices,
        referrals=referrals,
        transport_routes=routes,
        test_volumes=dict(payload.get("test_volumes", {})),
        test_volumes_by_type=payload.get("test_volumes_by_type"),
        device_type_volumes=_pipe_dict(payload.get("device_type_volumes"), 2),
        device_type_test_volumes=_pipe_dict(payload.get("device_type_test_volumes"), 3),
        device_active_tests=device_active,
        test_type_names=payload.get("test_type_names"),
        use_historical_lab_baseline=payload.get("use_historical_lab_baseline", False),
        config=config,
    )

    if goal_preset and catalogue_required(goal_preset) and catalogue is None:
        raise ValueError(
            f"catalogue is required for preset {goal_preset!r} — "
            "device_types and test_parameters must be merged before capability seeding"
        )
    if catalogue is not None:
        merge_catalogue_into_state(state, catalogue)

    manual_caps = {
        (row[0], row[1])
        for row in payload.get("manual_capability_overrides", [])
        if isinstance(row, (list, tuple)) and len(row) == 2
    }
    seed_facility_test_capabilities(state, db_capabilities=manual_caps)
    return state


def resolve_catalogue_path(network_path: Path, catalogue_path: Optional[Path] = None) -> Optional[Path]:
    """Match CLI auto-detect: explicit path, then {stem}.catalogue.json, then catalogue.json."""
    if catalogue_path is not None:
        p = Path(catalogue_path)
        return p if p.is_file() else None
    parent = network_path.parent
    stem = network_path.stem
    for candidate in (parent / f"{stem}.catalogue.json", parent / "catalogue.json"):
        if candidate.is_file():
            return candidate
    return None


def load_network_json(
    path_or_dict: Union[str, Path, Mapping[str, Any]],
    *,
    catalogue_path: Optional[Path] = None,
    goal_preset: Optional[str] = None,
) -> NetworkState:
    data = _read(path_or_dict)
    cat_data = None
    if isinstance(path_or_dict, (str, Path)):
        net_path = Path(path_or_dict)
        cat_path = resolve_catalogue_path(net_path, catalogue_path)
        if goal_preset and catalogue_required(goal_preset) and cat_path is None:
            raise ValueError(
                f"catalogue is required for preset {goal_preset!r} — "
                f"place {net_path.stem}.catalogue.json or catalogue.json beside the network file"
            )
        if cat_path is not None:
            cat_data = json.loads(cat_path.read_text(encoding="utf-8"))
    elif catalogue_path is not None and Path(catalogue_path).exists():
        cat_data = json.loads(Path(catalogue_path).read_text(encoding="utf-8"))
    return load_network_dict(data, catalogue=cat_data, goal_preset=goal_preset)
