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

"""Generate goal-specific starter JSON packs for semi-technical users."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from dno_core.domain.labels import PresetName, catalogue_required, domain_goal_name

README_TEMPLATE = """DNO network starter — {goal_label}

Files:
  network.json              Referring sites, testing sites, devices, demand, referrals
  network.catalogue.json    Device catalogue (optional for open_referral)

Goal: {goal_label} ({goal_desc})

Next steps:
  1. Replace fictional IDs/names with your own data (keep the same JSON keys).
  2. Read the field guide: docs/data-preparation.md (in the repo root README)
  3. dno optimize --network network.json --goal {goal_cli}
  4. dno optimize --network network.json --goal {goal_cli} --output results/

Terminology: referring site = facilities[], testing site = labs[],
annual testing demand = test_volumes, sample referral linkage = referrals[].
"""


def _referral_network_example() -> Tuple[dict, None]:
    # Minimal starter — same neutral naming as samples/demo/
    network = {
        "schema_version": 1,
        "project_id": "my-network",
        "labs": [
            {
                "id": "lab-north",
                "name": "North District Laboratory",
                "admin_area_1": "Sample Region",
                "admin_area_2": "North District",
                "latitude": "45.1200",
                "longitude": "-120.0000",
            },
            {
                "id": "lab-south",
                "name": "South District Laboratory",
                "admin_area_1": "Sample Region",
                "admin_area_2": "South District",
                "latitude": "44.8800",
                "longitude": "-120.0000",
            },
        ],
        "facilities": [
            {
                "id": "hf-a",
                "name": "Health Facility North 1",
                "admin_area_1": "Sample Region",
                "admin_area_2": "North District",
                "latitude": "45.1100",
                "longitude": "-120.0100",
            },
            {
                "id": "hf-b",
                "name": "Health Facility South 1",
                "admin_area_1": "Sample Region",
                "admin_area_2": "South District",
                "latitude": "44.8900",
                "longitude": "-120.0100",
            },
        ],
        "devices": [
            {
                "facility_id": "lab-north",
                "device_type": "GX 4",
                "device_id": "dev-n1",
                "max_shifts": 2,
                "max_tests_per_shift": 40,
            },
            {
                "facility_id": "lab-south",
                "device_type": "GX 4",
                "device_id": "dev-s1",
                "max_shifts": 2,
                "max_tests_per_shift": 40,
            },
        ],
        "referrals": [
            {"facility_id": "hf-a", "lab_id": "lab-north", "referral_count": 1200, "distance_km": 8.5},
            {"facility_id": "hf-b", "lab_id": "lab-south", "referral_count": 900, "distance_km": 6.2},
        ],
        "test_volumes": {"hf-a": 1200, "hf-b": 900},
    }
    return network, None


def _catalogue_example() -> dict:
    return {
        "device_types": [
            {
                "id": "dt-gx4",
                "name": "GX 4",
                "test_parameters": [
                    {
                        "test_type_id": "tt-mtb",
                        "max_tests_per_shift": 40,
                        "max_shifts": 2,
                        "cost_per_test": 12.5,
                    }
                ],
            }
        ],
        "mad_device_caps": {"GX 4": 4},
    }


def _balanced_example() -> Tuple[dict, dict]:
    network, _ = _referral_network_example()
    network["project_id"] = "example-balanced-network"
    for dev in network["devices"]:
        dev["device_type_id"] = "dt-gx4"
    network["manual_capability_overrides"] = [["lab-north", "tt-mtb"], ["lab-south", "tt-mtb"]]
    return network, _catalogue_example()


def _capacity_example() -> Tuple[dict, dict]:
    network, cat = _balanced_example()
    network["project_id"] = "example-testing-capacity"
    return network, cat


def write_template_pack(
    target_dir: Path,
    preset: PresetName,
    *,
    example: bool = True,
    force: bool = False,
) -> None:
    target_dir = target_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    net_path = target_dir / "network.json"
    cat_path = target_dir / "network.catalogue.json"
    readme_path = target_dir / "README.txt"

    if not force and any(p.exists() for p in (net_path, cat_path, readme_path)):
        raise FileExistsError(
            f"Refusing to overwrite existing files in {target_dir} — pass --force to replace"
        )

    if preset != "open_referral":
        raise ValueError(f"OSS supports open_referral templates only, not {preset!r}")
    network, catalogue = _referral_network_example()

    if not example:
        # Empty skeleton: same keys, minimal rows
        network = {
            "schema_version": 1,
            "project_id": "my-network",
            "labs": [],
            "facilities": [],
            "devices": [],
            "referrals": [],
            "test_volumes": {},
        }
        if catalogue_required(preset):
            network["manual_capability_overrides"] = []
        catalogue = _catalogue_example() if catalogue_required(preset) else None

    net_path.write_text(json.dumps(network, indent=2), encoding="utf-8")
    if catalogue is not None:
        cat_path.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")

    from dno_core.domain.labels import GOAL_DESCRIPTIONS

    readme_path.write_text(
        README_TEMPLATE.format(
            goal_label=domain_goal_name(preset),
            goal_desc=GOAL_DESCRIPTIONS[preset],
            goal_cli=domain_goal_name(preset),
        ),
        encoding="utf-8",
    )
