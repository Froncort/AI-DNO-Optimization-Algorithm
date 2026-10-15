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

"""Domain-facing labels for CLI and markdown outputs — JSON keys stay unchanged in the loader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional

PresetName = Literal["open_referral"]
FocusName = Literal["util", "access", "cost", "balanced"]

FOCUS_WEIGHTS: Dict[FocusName, Dict[str, float]] = {
    "balanced": {"utilization": 0.50, "accessibility": 0.50, "cost": 0.00},
    "util": {"utilization": 0.70, "accessibility": 0.30, "cost": 0.00},
    "access": {"utilization": 0.30, "accessibility": 0.70, "cost": 0.00},
    "cost": {"utilization": 0.33, "accessibility": 0.33, "cost": 0.34},
}

OPEN_REFERRAL_DEFAULT_WEIGHTS: Dict[str, float] = {
    "utilization": 0.0,
    "accessibility": 1.0,
    "cost": 0.0,
}

FOCUS_ALIASES: Dict[str, FocusName] = {
    "util": "util",
    "utilization": "util",
    "access": "access",
    "accessibility": "access",
    "distance": "access",
    "cost": "cost",
    "balanced": "balanced",
    "balance": "balanced",
}


@dataclass(frozen=True)
class ScenarioSpec:
    modes: tuple[str, ...]
    expansion_levers: Dict[str, bool]
    allow_conversions: bool


SCENARIO_SPECS: Dict[PresetName, ScenarioSpec] = {
    "open_referral": ScenarioSpec(
        modes=("redesign",),
        expansion_levers={"test_integration": False, "shift_increase": False, "add_device": False},
        allow_conversions=False,
    ),
}

GOAL_ALIASES: Dict[str, PresetName] = {
    "referral-network": "open_referral",
    "redesign": "open_referral",
    "open-referral": "open_referral",
    "open_referral": "open_referral",
}

GOAL_DESCRIPTIONS: Dict[PresetName, str] = {
    "open_referral": "Optimize sample referral assignments (access / distance)",
}

DOMAIN_GOAL_NAMES: Dict[PresetName, str] = {
    "open_referral": "referral-network",
}

GOAL_PHASES: Dict[PresetName, str] = {
    "open_referral": "redesign",
}

ACTION_DOMAIN_LABELS: Dict[str, str] = {
    "REDESIGN": "Sample referral change",
    "RELOCATION": "Device relocation",
    "CONVERSION": "Testing site upgrade",
    "EXPANSION": "Capacity addition",
    "TEST_INTEGRATION": "Test integration",
    "ADD_SHIFT": "Additional shift",
}


def _normalize_goal_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def parse_goal(value: str) -> PresetName:
    raw = value.strip()
    candidates = (
        raw,
        raw.lower(),
        _normalize_goal_key(raw),
        raw.lower().replace("-", "_"),
    )
    for key in candidates:
        if key in GOAL_ALIASES:
            return GOAL_ALIASES[key]
    allowed = sorted(set(GOAL_ALIASES.keys()))
    raise ValueError(f"Unknown goal {value!r}; use one of: {', '.join(allowed)}")


def parse_focus(value: str) -> FocusName:
    key = value.strip().lower()
    normalized = FOCUS_ALIASES.get(key)
    if normalized is None:
        allowed = sorted(FOCUS_ALIASES.keys())
        raise ValueError(f"Unknown focus {value!r}; use one of: {', '.join(allowed)}")
    return normalized


def default_focus(preset: PresetName) -> FocusName:
    return "access"


def objective_weights_for(
    preset: PresetName,
    focus: FocusName,
    *,
    focus_explicit: bool,
) -> Dict[str, float]:
    if preset == "open_referral" and not focus_explicit:
        return dict(OPEN_REFERRAL_DEFAULT_WEIGHTS)
    return dict(FOCUS_WEIGHTS[focus])


def format_focus_line(
    preset: PresetName,
    focus: FocusName,
    *,
    focus_explicit: bool,
    weights: Optional[Dict[str, float]] = None,
) -> str:
    ow = weights or objective_weights_for(preset, focus, focus_explicit=focus_explicit)
    util = ow.get("utilization", 0.0) * 100
    access = ow.get("accessibility", 0.0) * 100
    cost = ow.get("cost", 0.0) * 100
    label = focus if focus_explicit or preset != "open_referral" else f"{focus} (Open Referral default)"
    return f"{label} — {util:.0f}% utilization, {access:.0f}% accessibility, {cost:.0f}% cost"


def domain_goal_name(preset: PresetName) -> str:
    return "referral-network"


def catalogue_required(preset: PresetName) -> bool:
    return False
