"""
Business / POI reader for PTiles format (.business.ptiles).

Decodes business records from Overture Maps data. Provides Business,
BusinessHit dataclasses and BusinessReader with nearby(), get_in_cell(),
get_in_bounds(). Loads categories from a sidecar JSON file.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import struct
from dataclasses import dataclass
from enum import IntEnum

import h3
import zstandard as zstd

from ptiles.codec import (
    decode_varint,
    zigzag_decode,
    decode_string_u16,
    decode_string_u8,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
    decode_coords_u16,
    decode_index_v2,
    decode_merged_block_header,
)

logger = logging.getLogger("ptiles.business")


class OperatingStatus(IntEnum):
    OPEN = 0
    CLOSED = 1
    TEMPORARILY_CLOSED = 2


@dataclass(frozen=True, slots=True)
class Business:
    osm_id: int
    lat: float
    lon: float
    name: str
    category: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    brand: str | None = None
    operating_status: str | None = None
    emails: tuple[str, ...] = ()
    socials: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BusinessHit:
    business: Business
    distance_meters: float


def decode_business_record(data: bytes, offset: int) -> tuple[dict, int]:
    """Decode one business (POI) record from a block.

    Returns (business_dict, bytes_consumed).
    """
    pos = offset

    # osm_id: zigzag varint (single, NOT delta from prev)
    osm_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = zigzag_decode(osm_raw)

    # lon_micro, lat_micro
    lon_micro = struct.unpack_from("<i", data, pos)[0]
    lat_micro = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8

    # name (required, u16_str)
    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    # category_idx (u8, 0 = missing)
    category_idx = data[pos]
    pos += 1

    # flags
    flags = data[pos]
    pos += 1

    phone = None
    website = None
    address = None
    brand = None
    emails: list[str] = []
    socials: list[str] = []

    if flags & 0x01:
        phone, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x02:
        website, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        address, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x08:
        brand, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x10:
        # operating_status bit
        pass  # combined with flags ^ 0x10/0x12
    if flags & 0x20:
        emails_str, consumed = decode_string_u8(data, pos)
        pos += consumed
        emails = [e.strip() for e in emails_str.split(";") if e.strip()]
    if flags & 0x40:
        socials_str, consumed = decode_string_u8(data, pos)
        pos += consumed
        socials = [s.strip() for s in socials_str.split(";") if s.strip()]

    # Operating status encoding
    if (flags & 0x10) and not (flags & 0x02):
        operating_status = "closed"
    elif (flags & 0x10) and (flags & 0x02):
        operating_status = "temporarily_closed"
    else:
        operating_status = "open"

    return {
        "osm_id": osm_id,
        "lon": lon_micro / 100_000,
        "lat": lat_micro / 100_000,
        "name": name,
        "category_idx": category_idx,
        "phone": phone,
        "website": website,
        "address": address,
        "brand": brand,
        "operating_status": operating_status,
        "emails": tuple(emails),
        "socials": tuple(socials),
    }, pos - offset


def decode_block(data: bytes) -> list[dict]:
    """Decode all business records from a decompressed block.

    Format: { u32 record_len + record_body }*
    """
    businesses: list[dict] = []
    pos = 0
    while pos < len(data) - 4:
        try:
            record_len = struct.unpack_from("<I", data, pos)[0]
        except struct.error:
            break
        if record_len == 0:
            break
        pos += 4
        try:
            biz, consumed = decode_business_record(data, pos)
            businesses.append(biz)
            pos += record_len
        except Exception as e:
            logger.warning("Failed to decode business record: %s", e)
            pos += record_len
    return businesses


def _decode_business_attrs(data: bytes, pos: int) -> tuple[dict, int]:
    """Decode the v2 business attrs payload (everything after vertex_count + coords).

    v2 attrs order: osm_id varint, name u16_str, cat_idx u8, flags u8, optionals.
    """
    start = pos

    osm_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = zigzag_decode(osm_raw)

    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    category_idx = data[pos]
    pos += 1

    flags = data[pos]
    pos += 1

    phone = None
    website = None
    address = None
    brand = None
    emails: list[str] = []
    socials: list[str] = []

    if flags & 0x01:
        phone, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x02:
        website, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        address, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x08:
        brand, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x20:
        emails_str, consumed = decode_string_u8(data, pos)
        pos += consumed
        emails = [e.strip() for e in emails_str.split(";") if e.strip()]
    if flags & 0x40:
        socials_str, consumed = decode_string_u8(data, pos)
        pos += consumed
        socials = [s.strip() for s in socials_str.split(";") if s.strip()]

    if (flags & 0x10) and not (flags & 0x02):
        operating_status = "closed"
    elif (flags & 0x10) and (flags & 0x02):
        operating_status = "temporarily_closed"
    else:
        operating_status = "open"

    return {
        "osm_id": osm_id,
        "name": name,
        "category_idx": category_idx,
        "phone": phone,
        "website": website,
        "address": address,
        "brand": brand,
        "operating_status": operating_status,
        "emails": tuple(emails),
        "socials": tuple(socials),
    }, pos - start


def decode_business_record_v2(data: bytes, offset: int,
                              center_lon_micro: int,
                              center_lat_micro: int) -> tuple[dict, int]:
    """Decode one v2 business record body.

    Layout: u16 vertex_count + u16 coords + attrs.
    """
    pos = offset

    vertex_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    coords, consumed = decode_coords_u16(
        data, pos, center_lon_micro, center_lat_micro, vertex_count)
    pos += consumed
    lon, lat = coords[0]

    attrs, consumed = _decode_business_attrs(data, pos)
    pos += consumed

    biz = {
        "osm_id": attrs["osm_id"],
        "lon": lon,
        "lat": lat,
        "name": attrs["name"],
        "category_idx": attrs["category_idx"],
        "phone": attrs["phone"],
        "website": attrs["website"],
        "address": attrs["address"],
        "brand": attrs["brand"],
        "operating_status": attrs["operating_status"],
        "emails": attrs["emails"],
        "socials": attrs["socials"],
    }
    return biz, pos - offset


def decode_merged_block_for_cell(raw: bytes, cell_index: int) -> list[dict]:
    """Decode all v2 business records belonging to a specific cell within a merged block."""
    hdr = decode_merged_block_header(raw)
    center_lon_micro = hdr["center_lon_micro"]
    center_lat_micro = hdr["center_lat_micro"]
    cell_count = hdr["cell_count"]
    cell_offsets = hdr["cell_offsets"]
    record_data_start = hdr["record_data_offset"]

    if cell_index >= cell_count:
        return []

    start_rel = cell_offsets[cell_index][1]
    if cell_index + 1 < cell_count:
        end_rel = cell_offsets[cell_index + 1][1]
    else:
        end_rel = len(raw) - record_data_start

    abs_start = record_data_start + start_rel
    abs_end = record_data_start + end_rel

    businesses: list[dict] = []
    p = abs_start
    while p + 4 <= abs_end:
        record_len = struct.unpack_from("<I", raw, p)[0]
        if record_len == 0:
            break
        p += 4
        try:
            biz, _ = decode_business_record_v2(
                raw, p, center_lon_micro, center_lat_micro)
            businesses.append(biz)
        except Exception as e:
            logger.warning("Failed to decode v2 business record: %s", e)
        p += record_len
    return businesses


class BusinessReader:
    """Reader for .business.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str,
                 categories: list[str] | None = None):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        self._is_v2 = self._header["version"] >= 2

        # Load zstd dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Read spatial index (v1: 19-byte entries; v2: 37-byte + bbox + cell_index)
        f.seek(self._header["index_offset"])
        index_bytes = f.read(self._header["index_length"])
        if self._is_v2:
            self._index = decode_index_v2(index_bytes)
        else:
            self._index = read_index(index_bytes)

        # Detect relative offsets (v1 builds use relative; v2 uses absolute)
        self._relative_offsets = True
        if self._index:
            first_off = self._index[0]["block_offset"]
            self._relative_offsets = first_off < self._header["blocks_offset"]

        # Load categories
        if categories is not None:
            self._categories = categories
        else:
            self._categories = self._load_sidecar_categories()

        self._block_cache: dict[int, list[dict]] = {}
        self._block_cache_max = 5000
        # v2 only: raw decompressed merged block cache keyed by file offset.
        # Multiple H3 cells can share one merged block; this avoids redundant
        # decompression when walking neighbors.
        self._raw_block_cache: dict[int, bytes] = {}
        self._raw_block_cache_max = 256

    @classmethod
    def open(cls, path: str | os.PathLike, *,
             categories: list[str] | str | os.PathLike | None = None
             ) -> "BusinessReader":
        """Open a .business.ptiles file.

        If `categories` is None, the reader auto-locates
        `<basename>_categories.json` alongside the data file.
        """
        if isinstance(categories, (str, os.PathLike)):
            with open(categories) as f:
                data = json.load(f)
            cat_list = data.get("categories", data if isinstance(data, list) else [])
        else:
            cat_list = categories  # type: ignore
        f = open(path, "rb")
        return cls(f, str(path), categories=cat_list)

    @property
    def header(self) -> dict:
        return self._header

    def _load_sidecar_categories(self) -> list[str]:
        """Load categories from sidecar JSON file."""
        base = self._filepath
        if base.endswith(".ptiles"):
            base = base[:-7]
        sidecar_path = base + "_categories.json"
        if os.path.exists(sidecar_path):
            try:
                with open(sidecar_path) as f:
                    data = json.load(f)
                return data.get("categories", data if isinstance(data, list) else [])
            except Exception as e:
                logger.warning("Failed to load categories sidecar %s: %s", sidecar_path, e)
        return []

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _decompress(self, compressed: bytes) -> bytes | None:
        """Decompress a zstd block, trying dictionary first if available."""
        if self._dict_data:
            try:
                return decompress_block(compressed, self._dict_data)
            except Exception:
                pass
        try:
            return zstd.ZstdDecompressor().decompress(compressed)
        except Exception as e:
            logger.warning("Decompress failed: %s", e)
            return None

    def _read_raw_block_v2(self, file_offset: int, block_length: int) -> bytes | None:
        """v2 only: fetch + decompress a merged block, caching by file offset."""
        cached = self._raw_block_cache.get(file_offset)
        if cached is not None:
            return cached
        self._file.seek(file_offset)
        compressed = self._file.read(block_length)
        raw = self._decompress(compressed)
        if raw is None:
            return None
        if len(self._raw_block_cache) >= self._raw_block_cache_max:
            self._raw_block_cache.clear()
        self._raw_block_cache[file_offset] = raw
        return raw

    def _read_block(self, cell_int: int) -> list[dict]:
        """Read and decode a block for a given H3 cell."""
        # Check block cache first
        if cell_int in self._block_cache:
            return self._block_cache[cell_int]

        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return []
        file_offset = self._resolve_offset(entry["block_offset"])

        if self._is_v2:
            raw = self._read_raw_block_v2(file_offset, entry["block_length"])
            if raw is None:
                return []
            result = decode_merged_block_for_cell(raw, entry["cell_index"])
        else:
            self._file.seek(file_offset)
            compressed = self._file.read(entry["block_length"])
            raw = self._decompress(compressed)
            if raw is None:
                return []
            result = decode_block(raw)

        # Cache the result
        if len(self._block_cache) >= self._block_cache_max:
            self._block_cache.clear()
        self._block_cache[cell_int] = result

        return result

    def _dict_to_business(self, d: dict) -> Business:
        """Convert a decoded dict to a Business dataclass with resolved category."""
        category = None
        if d["category_idx"] > 0:
            idx = d["category_idx"] - 1  # 1-based in file
            if idx < len(self._categories):
                category = self._categories[idx]
        return Business(
            osm_id=d["osm_id"],
            lat=d["lat"],
            lon=d["lon"],
            name=d["name"],
            category=category,
            phone=d.get("phone"),
            website=d.get("website"),
            address=d.get("address"),
            brand=d.get("brand"),
            operating_status=d.get("operating_status", "open"),
            emails=d.get("emails", ()),
            socials=d.get("socials", ()),
        )

    def get_in_cell(self, cell: int | str) -> list[Business]:
        """Get all businesses in a single H3 res-7 cell."""
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        raw = self._read_block(cell_int)
        return [self._dict_to_business(d) for d in raw]

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[Business]:
        """Get all businesses within a lat/lon bounding box."""
        try:
            cells = h3.polygon_to_cells(
                [(min_lat, min_lon), (min_lat, max_lon),
                 (max_lat, max_lon), (max_lat, min_lon)],
                res=7,
            )
        except Exception:
            center_lat = (min_lat + max_lat) / 2
            center_lon = (min_lon + max_lon) / 2
            center_cell = h3.latlng_to_cell(center_lat, center_lon, 7)
            cells = h3.grid_disk(center_cell, 2)

        seen: set[int] = set()
        results: list[Business] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            raw = self._read_block(cell_int)
            for d in raw:
                if d["osm_id"] not in seen:
                    seen.add(d["osm_id"])
                    results.append(self._dict_to_business(d))
                    if len(results) >= limit:
                        return results
        return results

    def nearby(self, lat: float, lon: float, *,
               radius_meters: float = 500,
               limit: int = 10,
               category_prefix: str | None = None,
               exclude_closed: bool = False) -> list[BusinessHit]:
        """Find businesses near a point using H3 cell lookup.

        Dynamically determines the search radius in H3 rings based on
        the requested radius_meters.
        """
        cell = h3.latlng_to_cell(lat, lon, 7)
        radius_km = radius_meters / 1000.0
        rings_needed = max(0, int(round(radius_km / 1.8)))

        seen_cells: set[int] = set()
        hits: list[BusinessHit] = []

        for neighbor in h3.grid_disk(cell, rings_needed):
            cell_int = int(neighbor, 16)
            if cell_int in seen_cells:
                continue
            seen_cells.add(cell_int)

            raw = self._read_block(cell_int)
            if not raw:
                continue

            for d in raw:
                biz = self._dict_to_business(d)

                if exclude_closed and biz.operating_status == "closed":
                    continue
                if category_prefix is not None:
                    if biz.category is None or not biz.category.startswith(category_prefix):
                        continue

                dist = self._haversine(lat, lon, biz.lat, biz.lon)
                if dist <= radius_meters:
                    hits.append(BusinessHit(business=biz, distance_meters=dist))

        hits.sort(key=lambda h: h.distance_meters)
        return hits[:limit]

    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine distance in meters."""
        R = 6_371_000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        return R * c

    def close(self) -> None:
        self._file.close()
