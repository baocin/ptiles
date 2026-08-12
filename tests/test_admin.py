"""
Tests for ptiles.admin and scripts/build_admin.py.

The build-side tests cover the two pieces that used to be wrong: boundary
straddle flags (previously hardcoded to 0) and county naming (previously
"<NAME> County" for every jurisdiction, including Louisiana parishes).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import h3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ptiles.admin import AdminReader  # noqa: E402
import build_admin  # noqa: E402

ADMIN_PTILES = Path(
    os.environ.get(
        "ADMIN_PTILES",
        os.path.expanduser("~/kino/projects/ptiles/tiles/US.admin.ptiles"),
    )
)

# Straddles the TN/KY line north of Portland, TN.
TN_KY_BORDER = (36.6412, -86.5183)
# Deep inside Davidson County, TN — nowhere near a county or ZIP edge.
NASHVILLE_INTERIOR = (36.1627, -86.7816)
NEW_ORLEANS = (29.9511, -90.0715)


def _shapely_box_gdf(bounds_list):
    """Minimal GeoDataFrame of boxes, for the sampling test."""
    import geopandas as gpd
    from shapely.geometry import box

    return gpd.GeoDataFrame(geometry=[box(*b) for b in bounds_list], crs="EPSG:4269")


class TestBoundarySampling:
    def test_edge_cells_flagged_interior_cells_not(self):
        # Two boxes sharing the line lon = -86.5; cells on that line must be
        # reported, cells well inside must not.
        gdf = _shapely_box_gdf([(-87.0, 36.0, -86.5, 36.5), (-86.5, 36.0, -86.0, 36.5)])
        touched = build_admin.cells_touching_boundaries(gdf)

        on_edge = h3.api.numpy_int.latlng_to_cell(36.25, -86.5, 7)
        interior = h3.api.numpy_int.latlng_to_cell(36.25, -86.75, 7)

        assert on_edge in touched
        assert interior not in touched

    def test_sampling_leaves_no_gap_along_an_edge(self):
        # Every cell the shared edge passes through must be hit; walking the
        # edge at res-7 spacing and checking membership catches sampling gaps.
        gdf = _shapely_box_gdf([(-87.0, 36.0, -86.5, 36.5)])
        touched = build_admin.cells_touching_boundaries(gdf)
        for i in range(51):
            lat = 36.0 + i * 0.01
            assert h3.api.numpy_int.latlng_to_cell(lat, -86.5, 7) in touched

    def test_mark_boundary_cells_sets_expected_bits(self):
        gdf = _shapely_box_gdf([(-87.0, 36.0, -86.5, 36.5)])
        edge = int(h3.api.numpy_int.latlng_to_cell(36.25, -86.5, 7))
        interior = int(h3.api.numpy_int.latlng_to_cell(36.25, -86.75, 7))
        grid = [
            {"h3_cell": edge, "boundary_flags": 0},
            {"h3_cell": interior, "boundary_flags": 0},
        ]

        build_admin.mark_boundary_cells(grid, gdf, gdf, gdf, gdf)

        assert grid[0]["boundary_flags"] == (
            build_admin.STRADDLE_STATE
            | build_admin.STRADDLE_COUNTY
            | build_admin.STRADDLE_ZIP
            | build_admin.STRADDLE_TZ
        )
        assert grid[1]["boundary_flags"] == 0


@pytest.mark.skipif(
    not Path("/mnt/core/timeline-ptiles-cache/admin_data/counties").exists(),
    reason="TIGER county shapefile not available",
)
class TestCountyNaming:
    def test_polygon_names_use_the_source_jurisdiction_type(self):
        import geopandas as gpd

        counties = gpd.read_file(
            "/mnt/core/timeline-ptiles-cache/admin_data/counties/cb_2023_us_county_500k.shp"
        )
        by_state = counties.set_index("STATEFP")
        assert "Parish" in by_state.loc["22", "NAMELSAD"].iloc[0]
        # Virginia independent cities and Alaska census areas are not counties.
        assert any(n.endswith(" city") for n in by_state.loc["51", "NAMELSAD"])
        assert any("Census Area" in n for n in by_state.loc["02", "NAMELSAD"])


@pytest.mark.skipif(not ADMIN_PTILES.exists(), reason=f"{ADMIN_PTILES} not built")
class TestAdminReader:
    @pytest.fixture
    def reader(self) -> AdminReader:
        r = AdminReader.open(ADMIN_PTILES)
        yield r
        r.close()

    def test_query_nashville(self, reader: AdminReader):
        info = reader.query(*NASHVILLE_INTERIOR)
        assert info is not None
        assert info.state == "Tennessee"
        assert info.county == "Davidson"
        assert info.timezone == "America/Chicago"

    def test_state_border_cell_is_flagged(self, reader: AdminReader):
        info = reader.query(*TN_KY_BORDER)
        assert info is not None
        assert info.boundary_flags & build_admin.STRADDLE_STATE
        # A state edge is also a county edge.
        assert info.boundary_flags & build_admin.STRADDLE_COUNTY

    def test_interior_cell_is_not_flagged(self, reader: AdminReader):
        info = reader.query(*NASHVILLE_INTERIOR)
        assert not info.boundary_flags & build_admin.STRADDLE_STATE
        assert not info.boundary_flags & build_admin.STRADDLE_COUNTY

    def test_polygon_names_carry_the_jurisdiction_type(self, reader: AdminReader):
        names = {p.name for p in reader.polygons()}
        assert "Orleans Parish" in names
        assert "Fairbanks North Star Borough" in names
        assert "Southeast Fairbanks Census Area" in names
        assert "Falls Church city" in names
        # "Orleans County" is real (VT, NY); the parish must not be one.
        assert "Acadia County" not in names
        assert "Davidson County" in names

    def test_new_orleans_resolves(self, reader: AdminReader):
        info = reader.query(*NEW_ORLEANS)
        assert info.state == "Louisiana"
        assert info.county == "Orleans"
