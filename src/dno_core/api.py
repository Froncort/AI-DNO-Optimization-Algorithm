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

"""Public optimization API."""

import asyncio
from typing import Any, Callable, Dict, Optional

from dno_core.domain.referral_generator import backfill_unassigned_referrals
from dno_core.logging.progress import reset_progress_callback, set_progress_callback
from dno_core.models.config import EngineConfig
from dno_core.models.network_state import NetworkState
from dno_core.models.oss_goal import OssGoal
from dno_core.models.results import OptimizationResult
from dno_core.solvers.hierarchical import HierarchicalSolver


def optimize(
    state: NetworkState,
    goal: OssGoal,
    progress_callback: Optional[Callable[..., None]] = None,
) -> OptimizationResult:
    if not isinstance(goal, OssGoal):
        raise TypeError("OSS dno-core optimize() supports OssGoal only")
    if state.config is None:
        state.config = EngineConfig.get_defaults(state.project_id)
    state = backfill_unassigned_referrals(state)
    config = state.config
    scenario_config = goal.to_scenario_config()
    solver = HierarchicalSolver(config)
    token = set_progress_callback(progress_callback) if progress_callback else None
    try:
        return asyncio.run(solver.optimize(state, scenario_config, None))
    finally:
        if token is not None:
            reset_progress_callback(token)


def evaluate_baseline(state: NetworkState) -> Dict[str, Any]:
    from dno_core.evaluators.utilization import UtilizationEvaluator

    config = state.config or EngineConfig.get_defaults(state.project_id)
    return UtilizationEvaluator(config).evaluate(state).to_dict()


def evaluate_baseline_rich(state: NetworkState) -> Dict[str, Any]:
    from dno_core.evaluators.accessibility import AccessibilityEvaluator
    from dno_core.evaluators.utilization import UtilizationEvaluator
    from dno_core.evaluators.matrix_3x3 import Matrix3x3Evaluator
    from dno_core.calculators.cost import CostCalculator

    config = state.config or EngineConfig.get_defaults(state.project_id)
    utilization = UtilizationEvaluator(config).evaluate(state).to_dict()
    accessibility = AccessibilityEvaluator(config).evaluate(state)
    cost = dict(CostCalculator(config).calculate_cost_breakdown(state))
    matrix_3x3_baseline_self = Matrix3x3Evaluator(config).evaluate(
        state, state, [], goal=None
    ).to_dict()
    return {
        "schema_version": 1,
        "project_id": state.project_id,
        "utilization": utilization,
        "accessibility": accessibility,
        "cost": cost,
        "matrix_3x3_baseline_self": matrix_3x3_baseline_self,
    }


def backfill_referrals(state: NetworkState) -> NetworkState:
    return backfill_unassigned_referrals(state)
