"""
Address reader and offline geocoding (`{ST}.address_v2.ptiles`, magic PTILESD).

OSM building ways carrying `addr:housenumber`, keyed by H3 res-7 cell.

Two format generations, and the difference decides what is possible:

* **v2** stores i16 cell-relative coordinates per record. Both directions of
  geocoding work to about a metre.
* **v1** stored only (osm_id, housenumber, street) — the builder had the
  position, used it to pick the cell, and discarded it. An address could be
  located no better than its cell: measured on the shipped TN file, a median
  of 531 m and 2.6 km at the 90th percentile. This reader decodes v1 and
  reports `coords_exact=False` with the cell's bounding box, so a caller can
  see the difference rather than be told a box centre is an address.

v1 files also carry the *admin* magic (PTILESA), because a nine-byte magic was
truncated to seven. Both are accepted; see `ptiles.address.MAGIC`.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

import h3

from ptiles.codec import decode_varint, zigzag_decode
from ptiles.geometry import haversine_meters
from ptiles.points import PointLayerReader

logger = logging.getLogger("ptiles.address")

MAGIC = b"PTILESD"
LEGACY_MAGIC = b"PTILESA"

H3_RES = 7


@dataclass(frozen=True, slots=True)
class Address:
    osm_id: int
    housenumber: str
    street: str
    lat: float
    lon: float
    #: False for v1 records, where lat/lon is the centre of the address bbox
    #: for the whole cell rather than this address's own position.
    coords_exact: bool = True
    #: For v1, how far the true position could be from `lat`/`lon`. None when
    #: the record carries its own coordinates.
    accuracy_meters: float | None = None

    @property
    def formatted(self) -> str:
        return f"{self.housenumber} {self.street}".strip()


def _decode(data: bytes, pos: int, prev_osm_id: int, *, has_coords: bool,
            cell_center_micro: tuple[int, int]) -> tuple[dict, int, int]:
    """Decode one address record. Mirrors `enc` in scripts/build_address.py."""
    start = pos
    raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(raw)

    lon = lat = None
    if has_coords:
        off_lon, off_lat = struct.unpack_from("<hh", data, pos)
        pos += 4
        lon = (cell_center_micro[0] + off_lon) / 100_000
        lat = (cell_center_micro[1] + off_lat) / 100_000

    n = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    housenumber = data[pos:pos + n].decode("utf-8", "replace")
    pos += n

    n = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    street = data[pos:pos + n].decode("utf-8", "replace")
    pos += n

    return ({"osm_id": osm_id, "housenumber": housenumber, "street": street,
             "lon": lon, "lat": lat}, pos - start, osm_id)


class AddressReader(PointLayerReader):
    """Reader for the address layer, v1 and v2."""

    magic = b""  # checked below; v1 files carry the admin magic

    def __init__(self, f, filepath):
        super().__init__(f, filepath)
        got = self._header["magic"][:7]
        if got not in (MAGIC, LEGACY_MAGIC):
            raise ValueError(
                f"{filepath}: expected {MAGIC!r} (or the legacy {LEGACY_MAGIC!r}), "
                f"got {got!r}"
            )
        self._has_coords = self._header["version"] >= 2
        if not self._has_coords:
            logger.warning(
                "%s is address v1: records carry no coordinates, so positions "
                "are the per-cell bounding box (median ~531 m of error). "
                "Rebuild as v2 for geocoding.", filepath,
            )
        self._cell_center_cache: dict[int, tuple[int, int]] = {}

    @property
    def has_coordinates(self) -> bool:
        """True when records carry their own position, i.e. v2 or later."""
        return self._has_coords

    def _cell_center(self, cell_int: int) -> tuple[int, int]:
        c = self._cell_center_cache.get(cell_int)
        if c is None:
            lat, lon = h3.cell_to_latlng(format(cell_int, "x"))
            c = (round(lon * 100_000), round(lat * 100_000))
            self._cell_center_cache[cell_int] = c
        return c

    def decode_record(self, data: bytes, pos: int, prev_osm_id: int):
        # PointLayerReader calls this per record; the cell context is set by
        # get_in_cell just before the walk.
        return _decode(data, pos, prev_osm_id, has_coords=self._has_coords,
                       cell_center_micro=self._current_center)

    def get_in_cell(self, cell: int | str) -> list[Address]:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        self._current_center = self._cell_center(cell_int)
        raw = super().get_in_cell(cell_int)

        # v1 has no per-record position, so fall back to the cell's address
        # bounding box from the index entry, and say how wrong it may be.
        fallback_lat = fallback_lon = None
        accuracy = None
        if not self._has_coords:
            from ptiles.codec import binary_search_index
            entry = binary_search_index(self._index, cell_int)
            if entry and "min_lat" in entry:
                fallback_lat = (entry["min_lat"] + entry["max_lat"]) / 2 / 1e5
                fallback_lon = (entry["min_lon"] + entry["max_lon"]) / 2 / 1e5
                accuracy = haversine_meters(
                    entry["min_lat"] / 1e5, entry["min_lon"] / 1e5,
                    entry["max_lat"] / 1e5, entry["max_lon"] / 1e5,
                ) / 2
            else:
                lat, lon = h3.cell_to_latlng(format(cell_int, "x"))
                fallback_lat, fallback_lon, accuracy = lat, lon, 1400.0

        out: list[Address] = []
        for d in raw:
            if self._has_coords:
                out.append(Address(d["osm_id"], d["housenumber"], d["street"],
                                   d["lat"], d["lon"], True, None))
            else:
                out.append(Address(d["osm_id"], d["housenumber"], d["street"],
                                   fallback_lat, fallback_lon, False, accuracy))
        return out

    # --- reverse geocoding -------------------------------------------------

    def reverse(self, lat: float, lon: float, *, max_meters: float = 200.0,
                n: int = 1) -> list[tuple[Address, float]]:
        """Nearest addresses to a coordinate, nearest first.

        Returns (address, distance_metres) pairs. On a v1 file every candidate
        in a cell shares one position, so the ranking is meaningless — this
        raises rather than return a confidently wrong answer.
        """
        if not self._has_coords:
            raise ValueError(
                "reverse geocoding needs address v2: v1 records have no "
                "position, so every address in a cell would tie. Rebuild with "
                "scripts/build_address.py."
            )
        from ptiles.nearest import nearest

        found = nearest(self, lat, lon, n=n, max_meters=max_meters)
        return [(h.feature, h.distance_meters) for h in found]

    # --- forward geocoding -------------------------------------------------

    def geocode(self, street: str, housenumber: str | None = None, *,
                near: tuple[float, float] | None = None,
                max_rings: int = 12, limit: int = 10) -> list[Address]:
        """Find addresses by street, optionally narrowed to a house number.

        `near` bounds the work: with it, cells are read spiralling out from
        that point and the search stops once `limit` matches are in hand. It
        is the difference between reading a handful of blocks and reading the
        whole state, so pass it whenever the caller has any idea where to look
        — a map viewport centre will do.

        Without `near` this scans every cell in the file, which for a state is
        the entire address layer. Correct, but not cheap.
        """
        want_street = street.casefold().strip()
        want_number = housenumber.strip().casefold() if housenumber else None

        def matches(a: Address) -> bool:
            if want_street not in a.street.casefold():
                return False
            if want_number is not None and a.housenumber.casefold() != want_number:
                return False
            return True

        if near is not None:
            from ptiles.nearest import nearest
            found = nearest(self, near[0], near[1], predicate=matches,
                            n=limit, max_rings=max_rings)
            return [h.feature for h in found]

        out: list[Address] = []
        for entry in self._index:
            for a in self.get_in_cell(entry["h3_cell"]):
                if matches(a):
                    out.append(a)
                    if len(out) >= limit:
                        return out
        return out

    def streets(self, prefix: str = "", *, limit: int = 50) -> list[str]:
        """Distinct street names, optionally by prefix. Scans the file."""
        p = prefix.casefold()
        seen: set[str] = set()
        for entry in self._index:
            for a in self.get_in_cell(entry["h3_cell"]):
                if a.street and a.street.casefold().startswith(p):
                    seen.add(a.street)
                    if len(seen) >= limit:
                        return sorted(seen)
        return sorted(seen)
