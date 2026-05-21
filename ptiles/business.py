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


class BusinessReader:
    """Reader for .business.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str,
                 categories: list[str] | None = None):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)

        # Load zstd dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Read spatial index
        f.seek(self._header["index_offset"])
        index_bytes = f.read(self._header["index_length"])
        self._index = read_index(index_bytes)

        # Detect relative offsets
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

    def _read_block(self, cell_int: int) -> list[dict]:
        """Read and decode a block for a given H3 cell."""
        # Check block cache first
        if cell_int in self._block_cache:
            return self._block_cache[cell_int]

        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return []
        file_offset = self._resolve_offset(entry["block_offset"])
        self._file.seek(file_offset)
        compressed = self._file.read(entry["block_length"])
        raw = None
        if self._dict_data:
            try:
                raw = decompress_block(compressed, self._dict_data)
            except Exception:
                pass
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception as e:
                logger.warning("Decompress failed for cell %d: %s", cell_int, e)
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
