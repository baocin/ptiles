#!/usr/bin/env python3
"""
US States + DC metadata for PTILES generation.

FIPS codes, postal abbreviations, and tight bounding boxes (WGS84).
BBoxes are conservative (include buffer) to ensure no clipping at borders.
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
# Bounding boxes are padded slightly to avoid edge clipping
STATES: list[State] = [
    State("01", "AL", "Alabama", -88.5, 30.0, -84.9, 35.0),
    State("02", "AK", "Alaska", -173.0, 51.0, -130.0, 71.5),
    State("04", "AZ", "Arizona", -115.0, 31.3, -109.0, 37.0),
    State("05", "AR", "Arkansas", -94.6, 33.0, -89.0, 36.5),
    State("06", "CA", "California", -124.5, 32.5, -114.1, 42.0),
    State("08", "CO", "Colorado", -109.1, 37.0, -102.0, 41.0),
    State("09", "CT", "Connecticut", -73.7, 40.9, -71.8, 42.1),
    State("10", "DE", "Delaware", -75.8, 38.4, -75.0, 39.9),
    State("11", "DC", "District of Columbia", -77.2, 38.8, -76.9, 39.0),
    State("12", "FL", "Florida", -87.6, 24.4, -80.0, 31.0),
    State("13", "GA", "Georgia", -85.6, 30.0, -78.4, 35.0),
    State("15", "HI", "Hawaii", -160.5, 18.9, -154.8, 22.3),
    State("16", "ID", "Idaho", -117.0, 42.0, -111.0, 49.0),
    State("17", "IL", "Illinois", -91.5, 36.9, -87.0, 42.5),
    State("18", "IN", "Indiana", -88.1, 37.8, -84.8, 41.8),
    State("19", "IA", "Iowa", -96.6, 40.4, -90.1, 43.5),
    State("20", "KS", "Kansas", -102.1, 37.0, -94.6, 40.0),
    State("21", "KY", "Kentucky", -89.6, 36.5, -82.0, 39.2),
    State("22", "LA", "Louisiana", -94.1, 28.9, -88.8, 33.0),
    State("23", "ME", "Maine", -71.1, 43.0, -66.9, 47.5),
    State("24", "MD", "Maryland", -79.5, 37.9, -75.0, 39.8),
    State("25", "MA", "Massachusetts", -73.5, 41.2, -69.9, 42.9),
    State("26", "MI", "Michigan", -90.4, 41.7, -82.4, 48.3),
    State("27", "MN", "Minnesota", -97.3, 43.5, -89.5, 49.4),
    State("28", "MS", "Mississippi", -91.7, 30.0, -88.1, 35.0),
    State("29", "MO", "Missouri", -95.8, 35.9, -89.1, 40.6),
    State("30", "MT", "Montana", -116.1, 44.4, -104.0, 49.0),
    State("31", "NE", "Nebraska", -104.1, 40.0, -95.3, 43.0),
    State("32", "NV", "Nevada", -120.0, 35.0, -114.0, 42.0),
    State("33", "NH", "New Hampshire", -72.6, 42.7, -70.6, 45.3),
    State("34", "NJ", "New Jersey", -75.6, 38.9, -73.9, 41.4),
    State("35", "NM", "New Mexico", -109.1, 31.3, -103.0, 37.0),
    State("36", "NY", "New York", -79.8, 40.5, -71.8, 45.0),
    State("37", "NC", "North Carolina", -84.3, 33.8, -75.4, 36.6),
    State("38", "ND", "North Dakota", -104.1, 45.9, -96.5, 49.0),
    State("39", "OH", "Ohio", -84.8, 38.4, -80.5, 41.7),
    State("40", "OK", "Oklahoma", -103.0, 33.6, -94.4, 37.0),
    State("41", "OR", "Oregon", -124.6, 41.9, -116.5, 46.3),
    State("42", "PA", "Pennsylvania", -80.5, 39.7, -74.7, 42.3),
    State("44", "RI", "Rhode Island", -71.9, 41.1, -71.1, 42.0),
    State("45", "SC", "South Carolina", -83.4, 32.0, -78.5, 35.2),
    State("46", "SD", "South Dakota", -104.1, 42.5, -96.4, 45.9),
    State("47", "TN", "Tennessee", -90.3, 34.9, -81.6, 36.7),
    State("48", "TX", "Texas", -106.7, 25.8, -93.5, 36.5),
    State("49", "UT", "Utah", -114.1, 37.0, -109.0, 42.0),
    State("50", "VT", "Vermont", -73.5, 42.7, -71.5, 45.0),
    State("51", "VA", "Virginia", -83.7, 36.5, -75.2, 39.5),
    State("53", "WA", "Washington", -124.8, 45.5, -116.9, 49.0),
    State("54", "WV", "West Virginia", -82.7, 37.2, -77.7, 40.6),
    State("55", "WI", "Wisconsin", -93.0, 42.5, -86.8, 47.3),
    State("56", "WY", "Wyoming", -111.1, 41.0, -104.0, 45.0),
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
