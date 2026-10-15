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

"""Presentation layer — markdown/CSV sidecars; never mutates OptimizationResult.to_dict()."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from dno_core.domain.labels import (
    ACTION_DOMAIN_LABELS,
    DOMAIN_GOAL_NAMES,
    FocusName,
    GOAL_PHASES,
    PresetName,
    format_focus_line,
)
from dno_core.io.serializer import dump_result_json
from dno_core.io.validation import ValidationReport
from dno_core.models.results import OptimizationResult


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _action_type_name(action: Any) -> str:
    to_dict = getattr(action, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict) and data.get("action_type"):
            return str(data["action_type"])
    return getattr(action, "action_type", type(action).__name__)


def _count_actions(result: OptimizationResult) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for action in result.actions:
        key = _action_type_name(action)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _fmt_currency(amount: Any, currency: str = "INR") -> str:
    if amount is None:
        return "n/a"
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    sym = "₹" if currency == "INR" else f"{currency} "
    if abs(val) >= 1_000_000:
        return f"{sym}{val / 1_000_000:.2f}M"
    if abs(val) >= 1_000:
        return f"{sym}{val:,.0f}"
    return f"{sym}{val:.2f}"


def _fmt_pp(baseline_ratio: Any, final_ratio: Any) -> str:
    """Utilization change in percentage points (matches frontend dno-utils)."""
    try:
        delta = (float(final_ratio) - float(baseline_ratio)) * 100.0
        sign = "+" if delta >= 0 else ""
        return f"{sign}{delta:.1f} pts"
    except (TypeError, ValueError):
        return "—"


def _row(metric: str, baseline: str, optimized: str, change: str) -> str:
    return f"| {metric} | {baseline} | {optimized} | {change} |"


def build_summary_markdown(
    result: OptimizationResult,
    *,
    preset: PresetName,
    focus: FocusName = "balanced",
    focus_explicit: bool = False,
) -> str:
    base = result.baseline_metrics or {}
    final = result.final_metrics or {}
    m = result.matrix_3x3
    fin = m.financial
    impact = m.impact
    prog = m.programmatic
    counts = _count_actions(result)
    domain_goal = DOMAIN_GOAL_NAMES[preset]
    currency = fin.currency or "INR"

    lines = [
        "# Optimization summary",
        "",
        f"**Goal:** {domain_goal}",
        f"**RRE phases:** {GOAL_PHASES[preset]}",
        f"**Focus:** {format_focus_line(preset, focus, focus_explicit=focus_explicit)}",
        f"**Overall score:** {m.overall_score:.1f} / 100 — {m.recommendation or 'n/a'}",
        f"**Duration:** {result.execution_time_seconds:.1f}s · **Iterations:** {result.iterations} · "
        f"**Converged:** {'yes' if result.converged else 'no'} ({result.convergence_reason or 'n/a'})",
        "",
        "Comparison of the **current baseline network** vs the **optimized baseline** "
        "(same network after applying recommended sample referral changes). Origin demand is unchanged "
        "in Open Referral runs.",
        "",
        "## Baseline → optimized baseline",
        "",
        "| Metric | Baseline | Optimized baseline | Change |",
        "|--------|----------|-------------------|--------|",
    ]

    # Cost — primary financial comparison (matches frontend optimization results)
    cost_delta_pct = fin.cost_delta_percent
    cost_change = f"{cost_delta_pct:+.2f}%" if cost_delta_pct is not None else "—"
    lines.append(
        _row(
            "Total annual cost",
            _fmt_currency(fin.baseline_total_cost, currency),
            _fmt_currency(fin.total_cost, currency),
            cost_change,
        )
    )
    if fin.sample_transport_cost or fin.sample_transport_cost_optimized:
        transport_delta = fin.transport_cost_delta
        t_change = _fmt_currency(transport_delta, currency) if transport_delta else "—"
        lines.append(
            _row(
                "Sample transport (annual)",
                _fmt_currency(fin.sample_transport_cost, currency),
                _fmt_currency(fin.sample_transport_cost_optimized, currency),
                t_change,
            )
        )
    if fin.capex_cost and float(fin.capex_cost) > 0:
        lines.append(
            _row(
                "One-off CAPEX (this run)",
                "—",
                _fmt_currency(fin.capex_cost, currency),
                "—",
            )
        )

    # Access — accessibility score, ASD, RRE, coverage
    acc_imp = impact.accessibility_improvement
    acc_change = f"{acc_imp:+.1f}%" if acc_imp else "—"
    lines.append(
        _row(
            "Accessibility score (0–100)",
            f"{impact.baseline_accessibility:.1f}",
            f"{impact.final_accessibility:.1f}",
            acc_change,
        )
    )
    if impact.asd_baseline_km or impact.asd_optimized_km:
        # Lower ASD is better — positive change_km means improvement (distance reduced)
        asd_note = f"{impact.asd_change_km:+.1f} km"
        lines.append(
            _row(
                "Avg service distance (ASD)",
                f"{impact.asd_baseline_km:.1f} km",
                f"{impact.asd_optimized_km:.1f} km",
                asd_note,
            )
        )
    if impact.rre_score or impact.distance_saved_km:
        lines.append(
            _row(
                "RRE (referral route efficiency)",
                "—",
                f"{impact.rre_score * 100:.1f}%",
                f"{impact.distance_saved_km:.1f} km saved",
            )
        )
    if impact.pct_within_10km_baseline or impact.pct_within_10km_optimized:
        lines.append(
            _row(
                "Within 10 km",
                f"{impact.pct_within_10km_baseline:.1f}%",
                f"{impact.pct_within_10km_optimized:.1f}%",
                "—",
            )
        )
    if impact.pct_within_25km_baseline or impact.pct_within_25km_optimized:
        lines.append(
            _row(
                "Within 25 km",
                f"{impact.pct_within_25km_baseline:.1f}%",
                f"{impact.pct_within_25km_optimized:.1f}%",
                "—",
            )
        )
    if impact.pct_within_50km_baseline or impact.pct_within_50km_optimized:
        lines.append(
            _row(
                "Within 50 km",
                f"{impact.pct_within_50km_baseline:.1f}%",
                f"{impact.pct_within_50km_optimized:.1f}%",
                "—",
            )
        )

    # Utilization — percentage points, not relative %
    util_change = _fmt_pp(impact.baseline_avg_utilization, impact.final_avg_utilization)
    lines.append(
        _row(
            "Avg lab utilization",
            _pct(impact.baseline_avg_utilization),
            _pct(impact.final_avg_utilization),
            util_change,
        )
    )

    overutil_change = impact.overutilization_reduction
    overutil_delta = f"−{overutil_change}" if overutil_change > 0 else ("—" if overutil_change == 0 else str(overutil_change))
    lines.append(
        _row(
            "Overutilized testing sites (>95%)",
            str(impact.baseline_overutilized_labs),
            str(impact.final_overutilized_labs),
            overutil_delta,
        )
    )

    base_under = base.get("underutilized_count")
    final_under = final.get("underutilized_count")
    if base_under is not None and final_under is not None:
        under_delta = final_under - base_under
        lines.append(
            _row(
                "Underutilized testing sites (<40%)",
                str(base_under),
                str(final_under),
                f"{under_delta:+d}" if under_delta else "—",
            )
        )

    # Capacity row when impact matrix reports a capacity delta (unusual for Open Referral-only runs)
    if impact.capacity_change:
        lines.append(
            _row(
                "Testing capacity (tests/yr)",
                f"{impact.baseline_capacity:,}",
                f"{impact.final_capacity:,}",
                f"{impact.capacity_change:+,}",
            )
        )

    lines.extend(
        [
            "",
            "## 3×3 scorecard",
            "",
            f"| Axis | Score | Notes |",
            f"|------|-------|-------|",
            f"| Financial | {fin.score:.1f} | {fin.description} |",
            f"| Programmatic | {prog.score:.1f} | "
            f"{'Policy compliant' if prog.policy_compliant else 'Policy issues'} · "
            f"{prog.cross_boundary_actions} cross-boundary action(s) |",
            f"| Impact | {impact.score:.1f} | {impact.description} |",
        ]
    )

    if m.pros or m.cons:
        lines.extend(["", "## Assessment", ""])
        if m.pros:
            lines.append("**Strengths**")
            for p in m.pros:
                lines.append(f"- {p}")
            lines.append("")
        if m.cons:
            lines.append("**Trade-offs / risks**")
            for c in m.cons:
                lines.append(f"- {c}")
            lines.append("")

    lines.extend(["", "## Recommended actions", ""])
    if not counts:
        lines.append("No referral changes — optimized baseline matches current baseline on these metrics.")
    else:
        for raw, n in sorted(counts.items()):
            label = ACTION_DOMAIN_LABELS.get(raw, raw)
            lines.append(f"- **{label}:** {n}")
        lines.append("")
        lines.append(f"See `actions.md` for the full playbook ({result.total_actions} action(s)).")

    if result.impossibility_detected:
        lines.extend(["", "## Warnings", "", f"- {result.impossibility_reason or 'Impossibility detected'}"])

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- **Optimized baseline** = current network after applying the action list; not a separate scenario file.",
            "- Accessibility score combines ASD and coverage (higher = better access).",
            "- Utilization change is in **percentage points** (same as the AI DNO dashboard).",
            "- Full solver output: `result.json`. Action detail: `actions.md`.",
        ]
    )
    return "\n".join(lines)


def build_actions_markdown(result: OptimizationResult) -> str:
    lines = ["# Action playbook", ""]
    if not result.actions:
        lines.append("No actions recommended.")
        return "\n".join(lines)
    for i, action in enumerate(result.actions, 1):
        raw = _action_type_name(action)
        label = ACTION_DOMAIN_LABELS.get(raw, raw)
        lines.append(f"## {i}. {label}")
        rationale = getattr(action, "rationale", None)
        if rationale:
            lines.append(f"- **Rationale:** {rationale}")
        for key in sorted(vars(action).keys()):
            if key.startswith("_") or key in ("rationale",):
                continue
            val = getattr(action, key)
            if val is not None and val != "" and val != 0:
                lines.append(f"- **{key}:** {val}")
        lines.append("")
    return "\n".join(lines)


def build_actions_csv(result: OptimizationResult) -> str:
    buf = io.StringIO()
    fieldnames = [
        "raw_action_type",
        "domain_action",
        "details_json",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for action in result.actions:
        raw = _action_type_name(action)
        writer.writerow(
            {
                "raw_action_type": raw,
                "domain_action": ACTION_DOMAIN_LABELS.get(raw, raw),
                "details_json": json.dumps(vars(action), default=str),
            }
        )
    return buf.getvalue()


def build_metrics_csv(result: OptimizationResult) -> str:
    rows: List[Dict[str, Any]] = []
    m = result.matrix_3x3
    fin = m.financial
    impact = m.impact
    base = result.baseline_metrics or {}
    final = result.final_metrics or {}

    def add(metric: str, b: Any, o: Any, change: Any, unit: str, source: str) -> None:
        rows.append(
            {
                "metric": metric,
                "baseline": b,
                "optimized_baseline": o,
                "change": change,
                "unit": unit,
                "source": source,
            }
        )

    add(
        "Total annual cost",
        float(fin.baseline_total_cost),
        float(fin.total_cost),
        float(fin.cost_delta),
        fin.currency or "INR",
        "matrix_3x3.financial",
    )
    add(
        "Sample transport (annual)",
        float(fin.sample_transport_cost),
        float(fin.sample_transport_cost_optimized),
        float(fin.transport_cost_delta),
        fin.currency or "INR",
        "matrix_3x3.financial.cost_breakdown",
    )
    add(
        "Accessibility score",
        impact.baseline_accessibility,
        impact.final_accessibility,
        impact.accessibility_improvement,
        "score",
        "matrix_3x3.impact.accessibility",
    )
    add(
        "Average service distance (ASD)",
        impact.asd_baseline_km,
        impact.asd_optimized_km,
        impact.asd_change_km,
        "km",
        "matrix_3x3.impact.asd",
    )
    add(
        "RRE score",
        "",
        impact.rre_score * 100,
        impact.distance_saved_km,
        "percent / km saved",
        "matrix_3x3.impact.rre",
    )
    add(
        "Avg lab utilization",
        impact.baseline_avg_utilization,
        impact.final_avg_utilization,
        (impact.final_avg_utilization - impact.baseline_avg_utilization) * 100,
        "percentage points",
        "matrix_3x3.impact.utilization",
    )
    add(
        "Overutilized testing sites",
        impact.baseline_overutilized_labs,
        impact.final_overutilized_labs,
        -impact.overutilization_reduction,
        "count",
        "matrix_3x3.impact.overutilization",
    )
    add(
        "Underutilized testing sites",
        base.get("underutilized_count"),
        final.get("underutilized_count"),
        (final.get("underutilized_count") or 0) - (base.get("underutilized_count") or 0),
        "count",
        "metrics.baseline/final.underutilized_count",
    )
    for label, b, o in (
        ("Within 10 km", impact.pct_within_10km_baseline, impact.pct_within_10km_optimized),
        ("Within 25 km", impact.pct_within_25km_baseline, impact.pct_within_25km_optimized),
        ("Within 50 km", impact.pct_within_50km_baseline, impact.pct_within_50km_optimized),
    ):
        add(label, b, o, o - b if b is not None and o is not None else "", "percent", "matrix_3x3.impact.coverage")

    if impact.capacity_change:
        add(
            "Testing capacity",
            impact.baseline_capacity,
            impact.final_capacity,
            impact.capacity_change,
            "tests/year",
            "matrix_3x3.impact.capacity",
        )

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["metric", "baseline", "optimized_baseline", "change", "unit", "source"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_result_bundle(
    result: OptimizationResult,
    output_dir: Path,
    *,
    preset: PresetName,
    focus: FocusName = "balanced",
    focus_explicit: bool = False,
    validation_report: Optional[ValidationReport] = None,
    manifest_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Path]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_json = dump_result_json(result)
    raw_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    paths: Dict[str, Path] = {}
    paths["result.json"] = output_dir / "result.json"
    paths["result.json"].write_text(raw_json, encoding="utf-8")

    paths["summary.md"] = output_dir / "summary.md"
    paths["summary.md"].write_text(
        build_summary_markdown(result, preset=preset, focus=focus, focus_explicit=focus_explicit),
        encoding="utf-8",
    )

    paths["actions.md"] = output_dir / "actions.md"
    paths["actions.md"].write_text(build_actions_markdown(result), encoding="utf-8")

    paths["actions.csv"] = output_dir / "actions.csv"
    paths["actions.csv"].write_text(build_actions_csv(result), encoding="utf-8")

    paths["metrics.csv"] = output_dir / "metrics.csv"
    paths["metrics.csv"].write_text(build_metrics_csv(result), encoding="utf-8")

    brief = {
        "source": "dno_core.result_bundle.v1",
        "preset": DOMAIN_GOAL_NAMES[preset],
        "rre_phases": GOAL_PHASES[preset],
        "focus": format_focus_line(preset, focus, focus_explicit=focus_explicit),
        "raw_result_sha256": raw_hash,
        "overall_score": result.matrix_3x3.overall_score,
        "total_actions": result.total_actions,
    }
    paths["result-brief.json"] = output_dir / "result-brief.json"
    paths["result-brief.json"].write_text(json.dumps(brief, indent=2), encoding="utf-8")

    if validation_report is not None:
        paths["validation-report.md"] = output_dir / "validation-report.md"
        paths["validation-report.md"].write_text(validation_report.to_markdown(), encoding="utf-8")

    manifest = {"raw_result_sha256": raw_hash, "artifacts": sorted(paths.keys())}
    if manifest_extra:
        manifest.update(manifest_extra)
    paths["run-manifest.json"] = output_dir / "run-manifest.json"
    paths["run-manifest.json"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return paths


def format_terminal_scorecard(
    result: OptimizationResult,
    *,
    preset: PresetName,
    focus: FocusName = "balanced",
    focus_explicit: bool = False,
    output_dir: Optional[Path],
) -> str:
    m = result.matrix_3x3
    fin = m.financial
    impact = m.impact
    counts = _count_actions(result)
    currency = fin.currency or "INR"
    parts = []
    if counts.get("REDESIGN"):
        parts.append(f"{counts['REDESIGN']} referral changes")
    action_summary = ", ".join(parts) if parts else "none"

    cost_delta = fin.cost_delta_percent
    cost_line = (
        f"  Total cost:      {_fmt_currency(fin.baseline_total_cost, currency)} → "
        f"{_fmt_currency(fin.total_cost, currency)} ({cost_delta:+.2f}%)"
        if cost_delta is not None
        else f"  Total cost:      {_fmt_currency(fin.baseline_total_cost, currency)} → {_fmt_currency(fin.total_cost, currency)}"
    )

    lines = [
        f"✓ {DOMAIN_GOAL_NAMES[preset]} optimization complete ({result.execution_time_seconds:.0f}s)",
        f"  Phases: {GOAL_PHASES[preset]} · Focus: {format_focus_line(preset, focus, focus_explicit=focus_explicit)}",
        "",
        "  Baseline → Optimized baseline",
        cost_line,
        f"  Accessibility:   {impact.baseline_accessibility:.1f} → {impact.final_accessibility:.1f} "
        f"({impact.accessibility_improvement:+.1f}%)",
        f"  Avg utilization: {_pct(impact.baseline_avg_utilization)} → {_pct(impact.final_avg_utilization)} "
        f"({_fmt_pp(impact.baseline_avg_utilization, impact.final_avg_utilization)})",
        f"  Overutil labs:   {impact.baseline_overutilized_labs} → {impact.final_overutilized_labs}",
    ]
    if impact.asd_baseline_km or impact.asd_optimized_km:
        lines.append(
            f"  Avg distance:    {impact.asd_baseline_km:.1f} → {impact.asd_optimized_km:.1f} km"
        )
    lines.extend(
        [
            f"  Actions:         {action_summary}",
            f"  Score:           {m.overall_score:.0f}/100 — {m.recommendation or 'n/a'}",
        ]
    )
    if output_dir:
        lines.append(f"\n  Full report: {output_dir / 'summary.md'}")
    return "\n".join(lines)
