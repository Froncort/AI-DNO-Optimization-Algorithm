# AI-DNO Open Source Python API

AI-DNO Open Source can be integrated as a Python library when a repeatable workflow needs direct access to baseline metrics or optimization results. The package does not provide an HTTP server; applications can use the Python API or invoke the `dno` CLI as a subprocess.

## Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows, activate the environment with `.venv\Scripts\activate`.

## Public functions

| Function | Purpose |
| -------- | ------- |
| `load_network_json(path, goal_preset=...)` | Load `network.json` and its optional catalogue into a network state |
| `evaluate_baseline(state)` | Return headline utilization measures |
| `evaluate_baseline_rich(state)` | Return utilization, accessibility, cost, and baseline 3×3 measures |
| `optimize(state, goal)` | Run Open Referral redesign and return an `OptimizationResult` |
| `backfill_referrals(state)` | Estimate missing referral linkages before analysis |
| `dump_result_json(result)` | Serialize an `OptimizationResult` to formatted JSON |
| `OssGoal(...)` | Define the supported Open Referral goal |

## End-to-end example

```python
from pathlib import Path

from dno_core import (
    OssGoal,
    dump_result_json,
    evaluate_baseline_rich,
    load_network_json,
    optimize,
)

state = load_network_json(
    "samples/demo/network.json",
    goal_preset="open_referral",
)

# Measure the submitted network before redesign.
baseline = evaluate_baseline_rich(state)

# Open Referral is the supported optimization goal.
result = optimize(state, goal=OssGoal(preset="open_referral"))

Path("result.json").write_text(
    dump_result_json(result),
    encoding="utf-8",
)

print(baseline["utilization"])
print(result.to_summary())
```

The API uses `open_referral` as its preset name. The equivalent user-facing CLI goal is `referral-network`.

## CLI and API relationship

The CLI and Python API use the same optimization path:

1. Validate and load the network.
2. Create an `OssGoal` from the selected scenario and focus.
3. Call `optimize`.
4. Serialize the result.

Use the CLI when you want the complete result bundle and terminal progress:

```bash
dno optimize \
  -n samples/demo/network.json \
  -c samples/demo/network.catalogue.json \
  -g referral-network \
  --focus access \
  -o results/demo/
```

Use the Python API when another application needs to inspect the returned objects, control serialization, or combine DNO with an existing analysis workflow.

## Result formats

`optimize` returns an `OptimizationResult`. Its `to_dict()` method provides the machine-readable structure, while `to_summary()` provides a concise text summary.

`dump_result_json(result)` produces the same full JSON structure used for the CLI’s `result.json`. A CLI result bundle also includes:

- `summary.md` — stakeholder decision brief
- `actions.md` and `actions.csv` — referral redesign recommendations
- `metrics.csv` — baseline-versus-optimized measures
- `result-brief.json` — compact run metadata
- `validation-report.md` — input validation findings
- `run-manifest.json` — artifact list and result hash

For the input contract, see [data preparation](data-preparation.md). For interpretation guidance, see [Understanding outputs](walkthrough.md#part-c-understanding-outputs).
