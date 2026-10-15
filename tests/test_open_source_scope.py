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

"""The public package must contain only Open Referral solver capabilities."""

from __future__ import annotations


def main() -> None:
    from dno_core.domain.labels import SCENARIO_SPECS, parse_goal

    # Public builds expose the Open Referral scenario only.
    assert set(SCENARIO_SPECS.keys()) == {"open_referral"}
    try:
        parse_goal("full-rre")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    from dno_core.models.oss_goal import OssGoal

    cfg = OssGoal(preset="open_referral").to_scenario_config()
    assert set(cfg.optimization_modes).issubset({"redesign"})
    assert "expansion" not in cfg.optimization_modes

    from dno_core.solvers.hierarchical import HierarchicalSolver
    from dno_core.solvers import MILPDecompositionSolver, HierarchicalSolver as HS2

    assert HierarchicalSolver is HS2
    assert MILPDecompositionSolver is not None


if __name__ == "__main__":
    main()
    print("OK — open-source capability boundary")
