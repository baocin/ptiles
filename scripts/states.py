#!/usr/bin/env python3
"""
Region metadata for PTILES generation: US states + DC, plus non-US regions.

FIPS codes, postal abbreviations, and tight bounding boxes (WGS84).
BBoxes are derived from the Census 2023 cartographic boundary file
(cb_2023_us_state_500k) padded by 0.05 deg and rounded outward, so each box
is guaranteed to contain its state's full extent. Do not hand-edit: values
rounded to 1dp round DOWN on many borders and silently clip.

Alaska crosses the antimeridian (Census bounds -179.147 .. 179.778). A single
min/max lon pair cannot express that, so AK spans longitude fully; treat its
lon range as 'unbounded', not as a meaningful extent.
"""

from pathlib import Path
from typing import NamedTuple


class State(NamedTuple):
    fips: str  # 2-digit FIPS code as string (e.g., "47"); "" for non-US regions
    abbr: str  # USPS postal abbreviation (e.g., "TN")
    name: str  # Full name (e.g., "Tennessee")
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    pbf: str = ""  # Geofabrik basename; defaults to the slugified name

    @property
    def pbf_name(self) -> str:
        """Geofabrik extract basename, e.g. 'district-of-columbia'."""
        return self.pbf or self.name.lower().replace(" ", "-")


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


# Non-US regions. Deliberately NOT in STATES: every builder's --all iterates
# STATES, and a country-sized extract silently joining a 51-state run would be
# a nasty surprise. Reach these by name, e.g. --states JP.
# Every bbox below is read from the extract's own PBF header and rounded
# outward, never hand-derived. A box fitted to one layer's extremes (e.g. the
# road network) clips the outlying islands that other layers do reach.
NON_US: list[State] = [
    State("", "JP", "Japan", 122.55, 20.08, 154.48, 45.82, "japan"),
    # Geofabrik splits Japan into 8 regions, not 47 prefectures. Bboxes are read
    # from each extract's own PBF header, so they cover the outlying islands a
    # mainland-shaped guess would clip: Kanto reaches Minamitorishima (155.6E)
    # and Kyushu reaches Yonaguni (122.2E). The regions overlap slightly at their
    # seams, exactly as the Geofabrik US state extracts do.
    State("", "JP-HOKKAIDO", "Hokkaido", 137.93, 41.15, 146.25, 46.05, "hokkaido"),
    State("", "JP-TOHOKU", "Tohoku", 138.93, 36.52, 142.88, 41.65, "tohoku"),
    State("", "JP-KANTO", "Kanto", 134.04, 18.62, 155.61, 37.16, "kanto"),
    State("", "JP-CHUBU", "Chubu", 135.43, 34.26, 139.91, 38.91, "chubu"),
    State("", "JP-KANSAI", "Kansai", 133.96, 33.07, 137.66, 36.46, "kansai"),
    State("", "JP-CHUGOKU", "Chugoku", 129.89, 33.54, 134.53, 37.09, "chugoku"),
    State("", "JP-SHIKOKU", "Shikoku", 131.76, 32.22, 135.18, 34.66, "shikoku"),
    State("", "JP-KYUSHU", "Kyushu", 122.23, 20.72, 132.81, 35.1, "kyushu"),
]

US = "US"  # the country code the 51 state scopes belong to

REGIONS: list[State] = STATES + NON_US


def _check_non_us_scopes() -> None:
    """Refuse a non-US region whose scope would resolve back to a US state.

    26 ISO alpha-2 codes are also US state abbreviations -- CA is Canada and
    California, TN is Tunisia and Tennessee. A bare `CA` row here would publish
    Canada's country-wide files under exactly California's filenames, at the
    same path, overwriting them in the bucket with no error anywhere. Catch it
    at the table instead: use the alpha-3 code (`CAN`) or subdivisions (`CA-ON`).

    Checked against STATES directly, so this module keeps its only dependency
    being the standard library. ptiles.scopes.check_country_scope is the same
    rule for library callers.
    """
    us_abbrs = {s.abbr for s in STATES}
    for region in NON_US:
        head = region.abbr.split("-")[0]
        if len(head) == 2 and head in us_abbrs:
            raise ValueError(
                f"region {region.abbr!r} ({region.name}) collides with the US "
                f"state {head!r}. Use the ISO alpha-3 code (e.g. 'CAN') or "
                f"subdivision scopes (e.g. 'CA-ON')."
            )


_check_non_us_scopes()

# Geofabrik extracts have accumulated in three directories with two naming
# conventions. Search all of them rather than making every builder pick one.
PBF_DIRS = [
    Path("/mnt/core/timeline-ptiles-cache/raw"),
    Path("/mnt/core/timeline-ptiles-cache/2026-08-06/pbf"),
    Path("/mnt/aoi/kino/ptiles/pbfs"),
]


def country_of_scope(scope: str) -> str:
    """Country a scope belongs to, e.g. 'JP-KANTO' -> 'JP', 'TN' -> 'US'.

    Delegates to ptiles.scopes so the rule has one definition. Imported lazily
    because that package pulls in h3 and zstandard, and this module is otherwise
    standard-library only -- the import-time collision guard above deliberately
    keeps its own narrow check for that reason.
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parent.parent))
    from ptiles.scopes import country_of

    return country_of(scope)


def scopes_for_country(country: str, subdivisions: bool = False) -> list[str]:
    """The scopes to build for one country -- a *covering* set, not every scope.

    A country declares both forms when its layers need both: Japan has `JP` for
    the layers that fit one file and `JP-KANTO`..`JP-KYUSHU` for buildings,
    which do not. Returning all nine would build a country-wide rail file *and*
    eight regional ones covering the same track, so a client opening Japan sees
    every feature twice.

    So: the country-wide scope alone when one is declared, or the subdivisions
    when there is no country-wide scope. `subdivisions=True` inverts that, for a
    builder like build_state_v8 whose layer cannot fit a single file.
    """
    country = country.upper()
    whole, parts = [], []
    for region in REGIONS:
        try:
            if country_of_scope(region.abbr) != country:
                continue
        except ValueError:
            continue
        (parts if "-" in region.abbr else whole).append(region.abbr)

    if country == US:
        # The US has no country-wide scope; its 51 states are the covering set.
        return sorted(parts + whole)
    if subdivisions:
        return sorted(parts or whole)
    return sorted(whole or parts)



def get_state(abbr_or_fips: str) -> State | None:
    """Lookup by 2-letter abbr or 2-digit FIPS string. Includes non-US regions."""
    abbr_or_fips = abbr_or_fips.upper()
    for s in REGIONS:
        if s.abbr == abbr_or_fips or (s.fips and s.fips == abbr_or_fips):
            return s
    return None


def pbf_path(region: "State | str", prefer: Path | None = None) -> Path | None:
    """Locate a region's OSM extract. Returns None if no extract is on disk.

    `prefer` is searched first. These directories hold extracts of different
    vintages (raw/ is older than 2026-08-06/), so a caller that has always read
    one of them must keep passing it or its output silently changes snapshot.
    """
    if isinstance(region, str):
        region = get_state(region)
        if region is None:
            return None
    for d in ([prefer] if prefer else []) + PBF_DIRS:
        for fn in (f"{region.pbf_name}.osm.pbf", f"{region.pbf_name}-latest.osm.pbf"):
            p = d / fn
            if p.exists():
                return p
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
