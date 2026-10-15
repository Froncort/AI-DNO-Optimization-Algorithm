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

"""Single source of truth for facility tier vocabulary resolution."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dno_core.models.facility import Facility

CANONICAL_TIERS = ("LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4", "LEVEL_5")

TIER_POSITIONS = {"LEVEL_1": 5, "LEVEL_2": 4, "LEVEL_3": 3, "LEVEL_4": 2, "LEVEL_5": 1}

TIER_ALIASES = {
    "LAB_NATIONAL": "LEVEL_1", "LAB_DISTRICT": "LEVEL_2", "LAB_CHC": "LEVEL_3",
    "DMC": "LEVEL_4", "SCC": "LEVEL_5",
    "TERTIARY": "LEVEL_3", "SECONDARY": "LEVEL_4", "PRIMARY": "LEVEL_5",
    "SPECIALIZED": "LEVEL_1", "ADVANCED": "LEVEL_2", "INTERMEDIATE": "LEVEL_3",
    "BASIC": "LEVEL_4", "LOW": "LEVEL_5",
    "MC": "LEVEL_1", "MEDICAL COLLEGE": "LEVEL_1",
    "DH": "LEVEL_2", "DISTRICT HOSPITAL": "LEVEL_2", "SDH": "LEVEL_2",
    "CHC": "LEVEL_3", "PHC": "LEVEL_4",
    "1": "LEVEL_1", "2": "LEVEL_2", "3": "LEVEL_3", "4": "LEVEL_4", "5": "LEVEL_5",
    "L1": "LEVEL_1", "L2": "LEVEL_2", "L3": "LEVEL_3", "L4": "LEVEL_4", "L5": "LEVEL_5",
    "PRIMARY_HC": "LEVEL_1", "SECONDARY_HC": "LEVEL_2", "TERTIARY_HC": "LEVEL_3",
    "T1_SCC": "LEVEL_5", "T2_MICRO": "LEVEL_4", "T3_NAT_LAB": "LEVEL_1", "MOBILE_VAN": "LEVEL_5",
}


def effective_tier_raw(facility: "Facility") -> str | None:
    return facility.facility_tier or facility.infrastructure_level


def resolve_to_canonical(raw: str | None) -> str | None:
    if not raw:
        return None
    stripped = raw.strip()
    if stripped in CANONICAL_TIERS:
        return stripped
    upper = stripped.upper()
    if upper in TIER_ALIASES:
        return TIER_ALIASES[upper]
    if stripped in TIER_ALIASES:
        return TIER_ALIASES[stripped]
    return None


def canonical_tier_position(raw: str | None) -> int:
    canonical = resolve_to_canonical(raw)
    return TIER_POSITIONS.get(canonical, 0) if canonical else 0


def resolve_tier_key(raw: str | None, config_map: dict) -> str | None:
    if raw is None or not config_map:
        return None
    for key in (raw, raw.strip(), raw.upper(), raw.lower()):
        if key in config_map:
            return key
    canonical = resolve_to_canonical(raw)
    if canonical and canonical in config_map:
        return canonical
    return None


def resolve_tier_score(raw: str | None, score_map: dict) -> float:
    key = resolve_tier_key(raw, score_map)
    return float(score_map[key]) if key else 0.5


def is_restricted_low_tier(raw: str | None) -> bool:
    pos = canonical_tier_position(raw)
    return 0 < pos <= 2


def normalize_tier_config_key(k: str) -> str:
    stripped = str(k).strip()
    if stripped in CANONICAL_TIERS:
        return stripped
    canonical = resolve_to_canonical(stripped)
    return canonical if canonical else stripped


def _device_name_upper(device_name: str | None) -> str:
    return (device_name or "").upper()


def device_requires_ac(device_name: str | None) -> bool:
    """GeneXpert needs AC; TrueNAT does not (RULES_FOR_AI_DNO.md §V)."""
    n = _device_name_upper(device_name)
    return "GX" in n or "GENEXPERT" in n


def device_min_infrastructure_level(device_name: str | None) -> str | None:
    """
    Minimum site tier token for catalogue rows (RULES_FOR_AI_DNO.md §V).
    TrueNAT PHC+ (4); GX4 CHC+ (3); GX16/8 district+ (2).
    """
    n = _device_name_upper(device_name)
    if "TR" in n or "TRUENAT" in n:
        return "4"
    if "GX" in n or "GENEXPERT" in n:
        if any(x in n for x in ("16", " 8", "8 ", "VIII")):
            return "2"
        return "3"
    return None


def device_chooser_platform_rank(device_name: str | None) -> int:
    """Lower wins: TrueNAT before other before GeneXpert."""
    n = _device_name_upper(device_name)
    if "TR" in n or "TRUENAT" in n:
        return 0
    if "GX" in n or "GENEXPERT" in n:
        return 2
    return 1


def _is_known_tr_gx_device(device_name: str | None) -> bool:
    n = _device_name_upper(device_name)
    return any(x in n for x in ("TR", "TRUENAT", "GX", "GENEXPERT"))


def normalize_device_catalog_record(rec: dict) -> dict:
    """Override stale ingest min_infra on TR/GX rows at catalogue load time."""
    name = rec.get("name")
    if not _is_known_tr_gx_device(name):
        return rec
    canon_min = device_min_infrastructure_level(name)
    if canon_min is not None:
        rec["min_infrastructure_level"] = canon_min
    rec["requires_ac"] = device_requires_ac(name)
    return rec


if __name__ == "__main__":
    site_phc = canonical_tier_position("4")
    assert device_min_infrastructure_level("Tr Duo") == "4"
    assert device_min_infrastructure_level("GX 4") == "3"
    assert device_min_infrastructure_level("GX 16") == "2"
    assert canonical_tier_position(device_min_infrastructure_level("Tr Duo")) <= site_phc
    assert canonical_tier_position(device_min_infrastructure_level("GX 4")) > site_phc
    assert device_chooser_platform_rank("Tr Uno") < device_chooser_platform_rank("GX 4")
    rec = normalize_device_catalog_record(
        {"name": "Tr Duo", "min_infrastructure_level": "3", "requires_ac": True}
    )
    assert rec["min_infrastructure_level"] == "4" and rec["requires_ac"] is False
    print("infrastructure_tier self-check ok")
