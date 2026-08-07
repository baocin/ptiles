#!/usr/bin/env python3
"""
US States + DC metadata for PTILES generation.

FIPS codes, postal abbreviations, and tight bounding boxes (WGS84).
BBoxes are derived from the Census 2023 cartographic boundary file
(cb_2023_us_state_500k) padded by 0.05 deg and rounded outward, so each box
is guaranteed to contain its state's full extent. Do not hand-edit: values
rounded to 1dp round DOWN on many borders and silently clip.

Alaska crosses the antimeridian (Census bounds -179.147 .. 179.778). A single
min/max lon pair cannot express that, so AK spans longitude fully; treat its
lon range as 'unbounded', not as a meaningful extent.
"""

from typing import NamedTuple


class State(NamedTuple):
    fips: str  # 2-digit FIPS code as string (e.g., "47")
    abbr: str  # USPS postal abbreviation (e.g., "TN")
    name: str  # Full name (e.g., "Tennessee")
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


# All 50 states + District of Columbia
# Census-derived, padded 0.05 deg, rounded outward -- see module docstring
STATES: list[State] = [
    State("01", "AL", "Alabama", -88.53, 30.17, -84.83, 35.06),
    State("02", "AK", "Alaska", -180.0, 51.16, 180.0, 71.44),
    State("04", "AZ", "Arizona", -114.87, 31.28, -108.99, 37.06),
    State("05", "AR", "Arkansas", -94.67, 32.95, -89.59, 36.55),
    State("06", "CA", "California", -124.46, 32.48, -114.08, 42.06),
    State("08", "CO", "Colorado", -109.12, 36.94, -101.99, 41.06),
    State("09", "CT", "Connecticut", -73.78, 40.93, -71.73, 42.11),
    State("10", "DE", "Delaware", -75.84, 38.4, -74.99, 39.89),
    State("11", "DC", "District of Columbia", -77.17, 38.74, -76.85, 39.05),
    State("12", "FL", "Florida", -87.69, 24.47, -79.98, 31.06),
    State("13", "GA", "Georgia", -85.66, 30.3, -80.79, 35.06),
    State("15", "HI", "Hawaii", -178.39, 18.86, -154.75, 28.46),
    State("16", "ID", "Idaho", -117.3, 41.93, -110.99, 49.06),
    State("17", "IL", "Illinois", -91.57, 36.92, -87.44, 42.56),
    State("18", "IN", "Indiana", -88.15, 37.72, -84.73, 41.82),
    State("19", "IA", "Iowa", -96.69, 40.32, -90.09, 43.56),
    State("20", "KS", "Kansas", -102.11, 36.94, -94.53, 40.06),
    State("21", "KY", "Kentucky", -89.63, 36.44, -81.91, 39.2),
    State("22", "LA", "Louisiana", -94.1, 28.87, -88.76, 33.07),
    State("23", "ME", "Maine", -71.14, 42.92, -66.89, 47.51),
    State("24", "MD", "Maryland", -79.54, 37.86, -74.99, 39.78),
    State("25", "MA", "Massachusetts", -73.56, 41.18, -69.87, 42.94),
    State("26", "MI", "Michigan", -90.47, 41.64, -82.36, 48.29),
    State("27", "MN", "Minnesota", -97.29, 43.44, -89.44, 49.44),
    State("28", "MS", "Mississippi", -91.71, 30.12, -88.04, 35.05),
    State("29", "MO", "Missouri", -95.83, 35.94, -89.04, 40.67),
    State("30", "MT", "Montana", -116.1, 44.3, -103.98, 49.06),
    State("31", "NE", "Nebraska", -104.11, 39.94, -95.25, 43.06),
    State("32", "NV", "Nevada", -120.06, 34.95, -113.98, 42.06),
    State("33", "NH", "New Hampshire", -72.61, 42.64, -70.56, 45.36),
    State("34", "NJ", "New Jersey", -75.61, 38.87, -73.84, 41.41),
    State("35", "NM", "New Mexico", -109.11, 31.28, -102.95, 37.06),
    State("36", "NY", "New York", -79.82, 40.44, -71.8, 45.07),
    State("37", "NC", "North Carolina", -84.38, 33.79, -75.41, 36.64),
    State("38", "ND", "North Dakota", -104.1, 45.88, -96.5, 49.06),
    State("39", "OH", "Ohio", -84.88, 38.35, -80.46, 42.03),
    State("40", "OK", "Oklahoma", -103.06, 33.56, -94.38, 37.06),
    State("41", "OR", "Oregon", -124.62, 41.94, -116.41, 46.35),
    State("42", "PA", "Pennsylvania", -80.57, 39.66, -74.63, 42.32),
    State("44", "RI", "Rhode Island", -71.92, 41.09, -71.07, 42.07),
    State("45", "SC", "South Carolina", -83.41, 31.98, -78.49, 35.27),
    State("46", "SD", "South Dakota", -104.11, 42.42, -96.38, 46.0),
    State("47", "TN", "Tennessee", -90.37, 34.93, -81.59, 36.73),
    State("48", "TX", "Texas", -106.7, 25.78, -93.45, 36.56),
    State("49", "UT", "Utah", -114.11, 36.94, -108.99, 42.06),
    State("50", "VT", "Vermont", -73.49, 42.67, -71.41, 45.07),
    State("51", "VA", "Virginia", -83.73, 36.49, -75.19, 39.52),
    State("53", "WA", "Washington", -124.82, 45.49, -116.86, 49.06),
    State("54", "WV", "West Virginia", -82.7, 37.15, -77.66, 40.69),
    State("55", "WI", "Wisconsin", -92.94, 42.44, -86.75, 47.14),
    State("56", "WY", "Wyoming", -111.11, 40.94, -104.0, 45.06),
]


def get_state(abbr_or_fips: str) -> State | None:
    """Lookup by 2-letter abbr or 2-digit FIPS string."""
    abbr_or_fips = abbr_or_fips.upper()
    for s in STATES:
        if s.abbr == abbr_or_fips or s.fips == abbr_or_fips:
            return s
    return None


def state_bbox(s: State) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat)."""
    return (s.min_lon, s.min_lat, s.max_lon, s.max_lat)


def state_bbox_by_abbr(abbr: str) -> tuple[float, float, float, float] | None:
    """Return bbox for a state by its 2-letter abbreviation."""
    s = get_state(abbr)
    return state_bbox(s) if s else None


if __name__ == "__main__":
    print(f"Loaded {len(STATES)} states + DC")
    for s in STATES:
        print(
            f"  {s.fips} {s.abbr:2s} {s.name:20s} bbox=({s.min_lon:.1f},{s.min_lat:.1f})-({s.max_lon:.1f},{s.max_lat:.1f})"
        )
