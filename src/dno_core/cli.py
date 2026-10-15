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

"""Unified CLI — `dno` subcommands + legacy `dno-run` entry point."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from dno_core import OssGoal, dump_result_json, load_network_json, optimize
from dno_core.domain.labels import (
    GOAL_DESCRIPTIONS,
    GOAL_PHASES,
    FocusName,
    catalogue_required,
    default_focus,
    domain_goal_name,
    format_focus_line,
    parse_focus,
    parse_goal,
)
from dno_core.io.loader import resolve_catalogue_path
from dno_core.io.result_bundle import format_terminal_scorecard, write_result_bundle
from dno_core.io.templates import write_template_pack
from dno_core.io.validation import validate_network

DNO_CORE_ROOT = Path(__file__).resolve().parents[2]

DEMO_NETWORK = DNO_CORE_ROOT / "samples" / "demo" / "network.json"
DEMO_CATALOGUE = DNO_CORE_ROOT / "samples" / "demo" / "network.catalogue.json"

# Handler name so we can replace ourselves without stacking duplicates.
_CLI_LOG_HANDLER = "dno_cli_solver"


def _configure_run_logging(*, quiet: bool, verbose: bool) -> None:
    """Show solver progress on the CLI (algo already logs via stdlib; nothing else wires it)."""
    for name in ("dno_core", "dno_core.solvers"):
        lg = logging.getLogger(name)
        lg.handlers = [h for h in lg.handlers if getattr(h, "name", None) != _CLI_LOG_HANDLER]
        if name == "dno_core.solvers":
            lg.propagate = True

    if quiet:
        return

    # Default INFO on solvers = MILP subgraph / phase lines users expect during optimize.
    # -v: DEBUG on whole dno_core package.
    level = logging.DEBUG if verbose else logging.INFO
    target = logging.getLogger("dno_core" if verbose else "dno_core.solvers")
    target.setLevel(level)
    target.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _CLI_LOG_HANDLER
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)


def _demo_network_paths() -> Optional[tuple[Path, Optional[Path]]]:
    """OSS demo uses the committed sample under samples/demo/."""
    network = DEMO_NETWORK
    if network.is_file():
        catalogue = DEMO_CATALOGUE if DEMO_CATALOGUE.is_file() else None
        return network, catalogue
    print(
        f"error: demo network not found: {network}",
        file=sys.stderr,
    )
    return None


def _resolve_catalogue(network_path: Path, catalogue_arg: Optional[str]) -> Optional[Path]:
    if catalogue_arg:
        return Path(catalogue_arg).expanduser().resolve()
    return resolve_catalogue_path(network_path)


def _load_catalogue_dict(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_focus(args: argparse.Namespace, preset) -> tuple[FocusName, bool]:
    raw = getattr(args, "focus", None)
    if raw is None:
        return default_focus(preset), False
    return parse_focus(raw), True


def _run_optimize(
    network_path: Path,
    catalogue_path: Optional[Path],
    preset,
    *,
    focus: FocusName,
    focus_explicit: bool,
    output: Optional[Path],
    partition: bool,
    admin_level: str,
    allow_synthetic: bool,
    quiet: bool,
    verbose: bool,
) -> int:
    _configure_run_logging(quiet=quiet, verbose=verbose)
    cat_dict = _load_catalogue_dict(catalogue_path)
    report = validate_network(
        network_path,
        preset=preset,
        catalogue=cat_dict,
        allow_synthetic_referrals=allow_synthetic,
    )
    if not report.passed:
        print(report.to_markdown(), file=sys.stderr)
        return 1

    try:
        state = load_network_json(
            network_path,
            catalogue_path=catalogue_path,
            goal_preset=preset,
        )
        goal = OssGoal(
            preset=preset,
            focus=focus,
            focus_explicit=focus_explicit,
            partition_by_admin_area=partition,
            admin_area_level=admin_level,
        )
        stdout_target = io.StringIO() if quiet and not verbose else None
        with contextlib.redirect_stdout(stdout_target) if stdout_target else contextlib.nullcontext():
            result = optimize(state, goal=goal)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(output).expanduser() if output else None
    if out_dir and out_dir.suffix == ".json" and not out_dir.is_dir():
        # Legacy dno-run: single file output
        out_dir.write_text(dump_result_json(result), encoding="utf-8")
        if not quiet:
            print(
                format_terminal_scorecard(
                    result,
                    preset=preset,
                    focus=focus,
                    focus_explicit=focus_explicit,
                    output_dir=None,
                )
            )
            print(f"Wrote full result to {out_dir}")
    elif out_dir:
        write_result_bundle(
            result,
            out_dir,
            preset=preset,
            focus=focus,
            focus_explicit=focus_explicit,
            validation_report=report,
        )
        if not quiet:
            print(
                format_terminal_scorecard(
                    result,
                    preset=preset,
                    focus=focus,
                    focus_explicit=focus_explicit,
                    output_dir=out_dir,
                )
            )
    elif not quiet:
        print(
            format_terminal_scorecard(
                result,
                preset=preset,
                focus=focus,
                focus_explicit=focus_explicit,
                output_dir=None,
            )
        )

    if verbose:
        print(result.to_summary())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    preset = parse_goal(args.goal)
    try:
        focus, focus_explicit = _resolve_focus(args, preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    paths = _demo_network_paths()
    if paths is None:
        return 1
    network, catalogue_path = paths
    # Bundled demo ships a catalogue for cost breakdown in reports.
    cat_path = catalogue_path
    return _run_optimize(
        network,
        cat_path,
        preset,
        focus=focus,
        focus_explicit=focus_explicit,
        output=args.output,
        partition=args.partition_by_admin_area,
        admin_level=args.admin_area_level,
        allow_synthetic=False,
        quiet=args.quiet,
        verbose=args.verbose,
    )


def cmd_init(args: argparse.Namespace) -> int:
    preset = parse_goal(args.goal)
    try:
        write_template_pack(
            Path(args.directory),
            preset,
            example=not args.empty,
            force=args.force,
        )
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        focus, focus_explicit = _resolve_focus(args, preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created starter files in {Path(args.directory).resolve()}")
    print(f"Goal: {domain_goal_name(preset)} — {GOAL_DESCRIPTIONS[preset]}")
    print(f"Phases: {GOAL_PHASES[preset]}")
    print(f"Focus: {format_focus_line(preset, focus, focus_explicit=focus_explicit)}")
    return 0


def _run_check(
    network_path: Path,
    catalogue_path: Optional[Path],
    preset,
    *,
    report_path: Optional[Path],
    allow_synthetic: bool,
) -> int:
    report = validate_network(
        network_path,
        preset=preset,
        catalogue=_load_catalogue_dict(catalogue_path),
        allow_synthetic_referrals=allow_synthetic,
    )
    text = report.to_markdown()
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
        print(f"Wrote validation report to {report_path}")
    else:
        print(text)
    return 0 if report.passed else 1


def _run_baseline(
    network_path: Path,
    catalogue_path: Optional[Path],
    preset,
    output: Optional[Path] = None,
) -> int:
    import json
    from dno_core.api import evaluate_baseline, evaluate_baseline_rich

    try:
        state = load_network_json(network_path, catalogue_path=catalogue_path, goal_preset=preset)
        baseline = evaluate_baseline(state)
        rich = evaluate_baseline_rich(state)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if output is not None:
        out_dir = output if output.is_dir() or output.suffix == "" else output.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "baseline.json").write_text(
            json.dumps(rich, indent=2, default=str), encoding="utf-8"
        )
        util = rich.get("utilization") or {}
        acc = rich.get("accessibility") or {}
        cost = rich.get("cost") or {}
        m3 = rich.get("matrix_3x3_baseline_self") or {}
        lines = [
            "# Baseline summary",
            "",
            f"- Utilization: {util.get('overall_utilization', 0):.1%}",
            f"- Overutilized labs: {util.get('overutilized_count', 0)}",
            f"- Accessibility score: {acc.get('accessibility_score', acc.get('score', 'n/a'))}",
            f"- Total cost: {cost.get('total_cost', 'n/a')}",
            f"- 3×3 self-score: {m3.get('overall_score', 'n/a')}",
            "",
            "_Omitted vs product Metrics tab: lab-level util maps, DB test names, admin drill-down._",
            "_Dead EngineConfig knobs (relocation/expansion) remain in JSON for compat — unused in OSS._",
        ]
        (out_dir / "baseline-summary.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote baseline to {out_dir}/")
        return 0

    demand_hfs = sum(1 for v in state.test_volumes.values() if int(v) > 0)
    referred = {r.facility_id for r in state.referrals if r.facility_id}

    print(f"Goal: {domain_goal_name(preset)}")
    print(f"Phases: {GOAL_PHASES[preset]}")
    print("")
    print("Network baseline")
    print(f"  Referring sites:        {len(state.facilities)}")
    print(f"  Testing sites:          {len(state.labs)}")
    print(f"  Devices:                {len(state.devices)}")
    print(f"  Sample referral links:  {len(state.referrals)}")
    print(f"  Sites with demand:      {demand_hfs}")
    print(f"  Annual testing demand:  {baseline.get('total_demand', 0)}")
    print(f"  Testing capacity:       {baseline.get('total_capacity', 0)}")
    print(f"  Utilization:            {baseline.get('overall_utilization', 0):.1%}")
    print(f"  Overutilized labs:      {baseline.get('overutilized_count', 0)}")
    unassigned = [hf for hf, vol in state.test_volumes.items() if int(vol) > 0 and hf not in referred]
    if unassigned:
        print(f"  Unassigned demand:      {len(unassigned)} referring site(s)")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    if args.check_only and args.baseline_only:
        print("error: use --check-only or --baseline-only, not both", file=sys.stderr)
        return 1

    network_path = Path(args.network).expanduser().resolve()
    if not network_path.is_file():
        print(f"error: network file not found: {network_path}", file=sys.stderr)
        return 1

    try:
        preset = parse_goal(args.goal)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cat_path = _resolve_catalogue(network_path, args.catalogue)

    if args.check_only:
        report_path = None
        if args.output:
            out = Path(args.output).expanduser()
            report_path = out if out.suffix == ".md" else out / "validation-report.md"
        return _run_check(
            network_path,
            cat_path,
            preset,
            report_path=report_path,
            allow_synthetic=args.allow_synthetic_referrals,
        )

    if args.baseline_only:
        out = Path(args.output).expanduser() if args.output else None
        return _run_baseline(network_path, cat_path, preset, output=out)

    try:
        focus, focus_explicit = _resolve_focus(args, preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if catalogue_required(preset) and cat_path is None:
        print(
            f"error: catalogue required for {domain_goal_name(preset)!r} "
            f"(use -c or place {network_path.stem}.catalogue.json beside network)",
            file=sys.stderr,
        )
        return 1
    return _run_optimize(
        network_path,
        cat_path,
        preset,
        focus=focus,
        focus_explicit=focus_explicit,
        output=Path(args.output) if args.output else None,
        partition=args.partition_by_admin_area,
        admin_level=args.admin_area_level,
        allow_synthetic=args.allow_synthetic_referrals,
        quiet=args.quiet,
        verbose=args.verbose,
    )


_GOAL_HELP = "Scenario: referral-network (Open Referral — sample referral redesign only)"


def _add_goal_arg(parser: argparse.ArgumentParser, default: str = "referral-network") -> None:
    parser.add_argument("--goal", "-g", default=default, help=_GOAL_HELP)


def _add_focus_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--focus",
        default=None,
        help="Objective weights: util | access | cost | balanced (default: access for referral-network, balanced otherwise)",
    )


def _add_partition_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--partition-by-admin-area", action="store_true")
    parser.add_argument(
        "--admin-area-level",
        choices=["admin_area_1", "admin_area_2"],
        default="admin_area_1",
    )


def build_dno_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", "-q", action="store_true", help="Minimal terminal output (hide solver progress)")
    common.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG solver logs + post-run result summary",
    )

    parser = argparse.ArgumentParser(prog="dno", description="Diagnostic network optimization CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", parents=[common], help="Run bundled sample network")
    _add_goal_arg(p_demo, default="referral-network")
    _add_focus_arg(p_demo)
    p_demo.add_argument("--output", "-o", default=None, help="Output directory or JSON file")
    _add_partition_args(p_demo)

    p_init = sub.add_parser("init", parents=[common], help="Create starter network files")
    p_init.add_argument("directory", help="Target directory")
    _add_goal_arg(p_init, default="referral-network")
    _add_focus_arg(p_init)
    p_init.add_argument("--empty", action="store_true", help="Empty skeleton instead of fictional example")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")

    p_opt = sub.add_parser(
        "optimize",
        parents=[common],
        help="Run optimization (validates input first; use flags to stop early)",
    )
    p_opt.add_argument("--network", "-n", required=True)
    p_opt.add_argument("--catalogue", "-c", default=None)
    _add_goal_arg(p_opt)
    _add_focus_arg(p_opt)
    p_opt.add_argument("--output", "-o", default=None, help="Results directory, result.json, or validation-report.md")
    p_opt.add_argument("--check-only", action="store_true", help="Validate JSON only — do not run solver")
    p_opt.add_argument("--baseline-only", action="store_true", help="Show baseline metrics only — do not run solver")
    _add_partition_args(p_opt)
    p_opt.add_argument("--allow-synthetic-referrals", action="store_true")

    return parser


def build_dno_run_parser() -> argparse.ArgumentParser:
    """Legacy single-command interface."""
    parser = argparse.ArgumentParser(
        prog="dno-run",
        description="Run diagnostic network optimization on a network JSON file.",
    )
    parser.add_argument("--network", "-n", required=True)
    parser.add_argument("--catalogue", "-c", default=None)
    parser.add_argument("--goal", "-g", default="open_referral", help=_GOAL_HELP)
    _add_focus_arg(parser)
    parser.add_argument("--partition-by-admin-area", action="store_true")
    parser.add_argument(
        "--admin-area-level",
        choices=["admin_area_1", "admin_area_2"],
        default="admin_area_1",
    )
    parser.add_argument("--output", "-o", default=None)
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--allow-synthetic-referrals", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_dno_parser().parse_args(argv)
    return {
        "demo": cmd_demo,
        "init": cmd_init,
        "optimize": cmd_optimize,
    }[args.command](args)


def main_run(argv: Optional[list[str]] = None) -> int:
    args = build_dno_run_parser().parse_args(argv)
    network_path = Path(args.network).expanduser().resolve()
    if not network_path.is_file():
        print(f"error: network file not found: {network_path}", file=sys.stderr)
        return 1
    try:
        preset = parse_goal(args.goal)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cat_path = _resolve_catalogue(network_path, args.catalogue)
    if catalogue_required(preset) and cat_path is None:
        print(
            f"error: catalogue required for goal {args.goal!r}",
            file=sys.stderr,
        )
        return 1
    try:
        focus, focus_explicit = _resolve_focus(args, preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _run_optimize(
        network_path,
        cat_path,
        preset,
        focus=focus,
        focus_explicit=focus_explicit,
        output=Path(args.output) if args.output else None,
        partition=args.partition_by_admin_area,
        admin_level=args.admin_area_level,
        allow_synthetic=args.allow_synthetic_referrals,
        quiet=args.quiet,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
