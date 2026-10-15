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

"""Coverage-split computation for the CoverageGoal evaluation branch."""

from typing import Any, Dict, List, Optional

from dno_core.models.network_state import NetworkState
from dno_core.domain.transport_referral_utils import _is_onsite_referral, _referral_distance

_ROAD_DIST_FACTOR = 1.4


def _primary_referral_by_facility(state: NetworkState) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ref in state.referrals or []:
        fid = str(ref.facility_id)
        if fid not in out:
            out[fid] = ref
    return out


def compute_coverage_split(
    state: NetworkState,
    lab_map: Optional[Dict[str, str]] = None,
    threshold_km: float = 20.0,
    volume_state: Optional[NetworkState] = None,
) -> Dict[str, Any]:
    vols = (
        volume_state.test_volumes
        if volume_state is not None
        else state.test_volumes
    )
    within_f = beyond_f = onsite_f = 0
    within_s = beyond_s = onsite_s = 0
    non_onsite_distances: List[float] = []

    slice_scoped = volume_state is not None
    by_fac = _primary_referral_by_facility(state)
    for fid, ref in by_fac.items():
        vol = int(vols.get(fid, 0) or 0)
        if slice_scoped and vol <= 0:
            continue
        is_onsite = _is_onsite_referral(fid, ref, lab_map, state=state)
        if is_onsite:
            onsite_f += 1
            onsite_s += vol
        else:
            eff = _referral_distance(state, ref) * _ROAD_DIST_FACTOR
            non_onsite_distances.append(eff)
            if eff <= threshold_km:
                within_f += 1
                within_s += vol
            else:
                beyond_f += 1
                beyond_s += vol

    for lab_fid in state.labs:
        if str(lab_fid) in {str(k) for k in by_fac}:
            continue
        vol = int(vols.get(lab_fid, 0) or 0)
        if vol > 0:
            onsite_f += 1
            onsite_s += vol

    avg = round(sum(non_onsite_distances) / len(non_onsite_distances), 4) if non_onsite_distances else 0.0

    return {
        "within_facilities": within_f,
        "beyond_facilities": beyond_f,
        "onsite_facilities": onsite_f,
        "total_in_scope_facilities": within_f + beyond_f + onsite_f,
        "within_samples": within_s,
        "beyond_samples": beyond_s,
        "onsite_samples": onsite_s,
        "avg_access_distance_km": avg,
    }
