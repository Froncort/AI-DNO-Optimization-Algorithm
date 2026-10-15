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

"""
Distance calculator using Haversine formula.
"""

import math
from functools import lru_cache
from typing import Tuple, Optional
from decimal import Decimal

from dno_core.models.facility import Facility


# Pure Haversine memoization — same coords/mode always yield the same km value.
_CIRCUITRY_FACTORS = {
    "aerial": 1.4,
    "road": 1.6,
    "geodesic": 1.0,
}
_DEFAULT_MODE = "aerial"
_EARTH_RADIUS_KM = 6371


@lru_cache(maxsize=100_000)
def _haversine_cached(
    lat1: float, lon1: float, lat2: float, lon2: float, mode: str
) -> float:
    """Haversine distance with circuity factor; bounded cache across MILP solves."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    circuitry = _CIRCUITRY_FACTORS.get(mode, _CIRCUITRY_FACTORS[_DEFAULT_MODE])
    return _EARTH_RADIUS_KM * c * circuitry


class DistanceCalculator:
    """
    Calculate distances between facilities using Haversine formula.

    Distance modes:
    - 'aerial': Haversine × 1.4 circuitry factor (default, standard road approximation)
    - 'road': Haversine × 1.6 circuitry factor (higher approximation for road networks
              in hilly/rural terrain typical of Indian states like Uttarakhand/Assam)
    """
    
    EARTH_RADIUS_KM = 6371
    CIRCUITRY_FACTORS = {
        "aerial": 1.4,
        "road": 1.6,
        "geodesic": 1.0,
    }
    DEFAULT_MODE = "aerial"
    
    @classmethod
    def haversine_distance(
        cls,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
        mode: str = "aerial"
    ) -> float:
        """
        Calculate Haversine distance between two points with a circuitry factor.
        
        Args:
            lat1, lon1: First point coordinates
            lat2, lon2: Second point coordinates
            mode: Distance mode — 'aerial' (×1.4) or 'road' (×1.6)
            
        Returns:
            Distance in kilometers
        """
        return _haversine_cached(lat1, lon1, lat2, lon2, mode)
    
    @classmethod
    def facility_distance(
        cls,
        facility1: Facility,
        facility2: Facility,
        mode: str = "aerial"
    ) -> Optional[float]:
        """
        Calculate distance between two facilities.
        
        Args:
            facility1, facility2: Facilities to measure between
            mode: Distance mode — 'aerial' or 'road'
            
        Returns:
            Distance in km, or None if coordinates missing
        """
        if not facility1.has_coordinates or not facility2.has_coordinates:
            return None
        
        return cls.haversine_distance(
            float(facility1.latitude),
            float(facility1.longitude),
            float(facility2.latitude),
            float(facility2.longitude),
            mode=mode
        )

    @classmethod
    def facility_geodesic_km(
        cls,
        facility1: Facility,
        facility2: Facility,
    ) -> Optional[float]:
        """
        Great-circle (Haversine) distance in km with **no** circuity factor.

        Matches ``dno_transport_routes.distance_km`` and historical referral rows.
        Apply ×1.4 (or other circuity) in costing / reporting, not here.
        """
        if not facility1.has_coordinates or not facility2.has_coordinates:
            return None
        return cls.haversine_distance(
            float(facility1.latitude),
            float(facility1.longitude),
            float(facility2.latitude),
            float(facility2.longitude),
            mode="geodesic",
        )
    
    @classmethod
    def distance_from_coords(
        cls,
        facility: Facility,
        lat: float,
        lon: float,
        mode: str = "aerial"
    ) -> Optional[float]:
        """
        Calculate distance from facility to arbitrary coordinates.
        """
        if not facility.has_coordinates:
            return None
        
        return cls.haversine_distance(
            float(facility.latitude),
            float(facility.longitude),
            lat,
            lon,
            mode=mode
        )
    
    @classmethod
    def is_within_distance(
        cls,
        facility1: Facility,
        facility2: Facility,
        max_distance_km: float
    ) -> bool:
        """
        Check if two facilities are within a given distance.
        """
        distance = cls.facility_distance(facility1, facility2)
        if distance is None:
            return False
        return distance <= max_distance_km
    
    @classmethod
    def get_distance_band(cls, distance_km: float) -> str:
        """
        Categorize distance into a band.
        
        Bands:
        - "0-10": Within 10km
        - "10-25": 10-25km
        - "25-50": 25-50km
        - "50-100": 50-100km
        - "100+": Over 100km
        """
        if distance_km <= 10:
            return "0-10"
        elif distance_km <= 25:
            return "10-25"
        elif distance_km <= 50:
            return "25-50"
        elif distance_km <= 100:
            return "50-100"
        else:
            return "100+"
