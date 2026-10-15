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

"""Validate-only network checks — never invokes the optimizer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dno_core.domain.labels import PresetName, catalogue_required
from dno_core.io.loader import load_network_dict


@dataclass
class ValidationFinding:
    level: str  # error | warning | info
    path: str
    message: str
    source: str = "network.json"


@dataclass
class ValidationReport:
    passed: bool
    findings: List[ValidationFinding] = field(default_factory=list)
    preset: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> List[ValidationFinding]:
        return [f for f in self.findings if f.level == "error"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "preset": self.preset,
            "summary": self.summary,
            "findings": [
                {"level": f.level, "path": f.path, "message": f.message, "source": f.source}
                for f in self.findings
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Validation report",
            "",
            f"**Result:** {'PASS' if self.passed else 'FAIL'}",
        ]
        if self.preset:
            lines.append(f"**Goal:** {self.preset}")
        lines.append("")
        if self.summary:
            lines.append("## Network summary")
            for key, val in self.summary.items():
                lines.append(f"- **{key}:** {val}")
            lines.append("")
        if not self.findings:
            lines.append("No issues found.")
            return "\n".join(lines)
        lines.append("## Findings")
        for f in self.findings:
            lines.append(f"- **[{f.level.upper()}]** `{f.path}` — {f.message}")
        return "\n".join(lines)


def _load_raw(path_or_dict: Union[str, Path, Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(path_or_dict, dict):
        return path_or_dict
    return json.loads(Path(path_or_dict).read_text(encoding="utf-8"))


def validate_network(
    path_or_dict: Union[str, Path, Dict[str, Any]],
    *,
    preset: PresetName,
    catalogue: Optional[Dict[str, Any]] = None,
    allow_synthetic_referrals: bool = False,
) -> ValidationReport:
    findings: List[ValidationFinding] = []
    raw = _load_raw(path_or_dict)

    try:
        state = load_network_dict(raw, catalogue=catalogue, goal_preset=preset)
    except Exception as exc:  # noqa: BLE001 — surface loader errors as validation findings
        findings.append(ValidationFinding("error", "$", str(exc)))
        return ValidationReport(passed=False, findings=findings, preset=preset)

    hf_ids = set(state.facilities.keys())
    lab_ids = set(state.labs.keys())
    device_ids: set[str] = set()

    for i, dev in enumerate(raw.get("devices", [])):
        did = dev.get("device_id", f"devices[{i}]")
        if did in device_ids:
            findings.append(ValidationFinding("error", f"devices[{i}].device_id", f"duplicate device_id {did!r}"))
        device_ids.add(did)
        fid = dev.get("facility_id")
        if fid and fid not in lab_ids:
            findings.append(
                ValidationFinding(
                    "error",
                    f"devices[{i}].facility_id",
                    f"{fid!r} is not a testing site (labs[].id)",
                )
            )

    # One active referral destination per referring site (v1 contract).
    ref_by_hf: Dict[str, int] = {}
    for i, ref in enumerate(raw.get("referrals", [])):
        hf = ref.get("facility_id")
        if not hf:
            continue
        ref_by_hf[hf] = ref_by_hf.get(hf, 0) + 1
        if ref.get("lab_id") and ref["lab_id"] not in lab_ids:
            findings.append(
                ValidationFinding("error", f"referrals[{i}].lab_id", f"unknown testing site {ref['lab_id']!r}")
            )
        if hf and hf not in hf_ids:
            findings.append(
                ValidationFinding("error", f"referrals[{i}].facility_id", f"unknown referring site {hf!r}")
            )
    for hf, count in ref_by_hf.items():
        if count > 1:
            findings.append(
                ValidationFinding(
                    "error",
                    f"referrals (facility_id={hf})",
                    "more than one active testing-site linkage per referring site (v1 supports one lab per HF)",
                )
            )

    test_volumes = raw.get("test_volumes") or {}
    total_demand = sum(int(v) for v in test_volumes.values())
    referred_hfs = set(ref_by_hf.keys())
    for hf_id, vol in test_volumes.items():
        if int(vol) <= 0:
            continue
        if hf_id not in hf_ids:
            findings.append(ValidationFinding("error", f"test_volumes.{hf_id}", "unknown referring site"))
        elif hf_id not in referred_hfs:
            if allow_synthetic_referrals:
                findings.append(
                    ValidationFinding(
                        "warning",
                        f"test_volumes.{hf_id}",
                        "positive demand but no referral linkage — synthetic backfill will run at optimize time",
                    )
                )
            else:
                findings.append(
                    ValidationFinding(
                        "error",
                        f"test_volumes.{hf_id}",
                        "positive annual testing demand but no sample referral linkage",
                    )
                )

    if catalogue_required(preset) and catalogue is None:
        findings.append(
            ValidationFinding(
                "error",
                "catalogue",
                f"catalogue JSON required for goal {preset!r} (device_types and test_parameters)",
            )
        )

    if not lab_ids:
        findings.append(ValidationFinding("error", "labs", "at least one testing site (lab) is required"))
    if not raw.get("devices"):
        findings.append(ValidationFinding("warning", "devices", "no devices declared — capacity will be zero"))

    summary = {
        "referring_sites": len(hf_ids),
        "testing_sites": len(lab_ids),
        "devices": len(device_ids),
        "referral_linkages": len(raw.get("referrals", [])),
        "annual_testing_demand": total_demand,
    }

    passed = not any(f.level == "error" for f in findings)
    return ValidationReport(passed=passed, findings=findings, preset=preset, summary=summary)
