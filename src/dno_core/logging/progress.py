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

"""Progress reporting shim — replaces the file-writing OptimizationLogger."""

from typing import Any, Callable, Dict, Optional
import contextvars

# Set by api.optimize() for the duration of one run; None means silent success.
_progress_callback: contextvars.ContextVar[Optional[Callable[..., None]]] = (
    contextvars.ContextVar("dno_core_progress_callback", default=None)
)


class _ProgressLogger:
    def log(
        self,
        project_id: str,
        step: str,
        status: str,
        details: Dict[str, Any],
        iteration: Optional[int] = None,
        *,
        job_id: Optional[str] = None,
        scenario_id: Optional[str] = None,
    ) -> None:
        cb = _progress_callback.get()
        if cb is None:
            return
        try:
            cb(step, status, details, iteration)
        except Exception:
            pass


def set_progress_callback(cb: Optional[Callable[..., None]]) -> contextvars.Token:
    return _progress_callback.set(cb)


def reset_progress_callback(token: contextvars.Token) -> None:
    _progress_callback.reset(token)


optimization_logger = _ProgressLogger()
