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
import os
import re
import struct
from dataclasses import dataclass
from enum import IntEnum

import h3
import zstandard as zstd

from ptiles.geometry import haversine_meters
from ptiles.codec import (
    decode_varint,
    zigzag_decode,
    decode_string_u16,
    decode_string_u8,
    read_header,
    read_index_auto,
    binary_search_index,
    decompress_block,
    decode_coords_u16,
    decode_merged_block_header,
    INDEX_ENTRY_SIZE_V2,
)
from ptiles.reader import BoundaryMixin

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
    # v5: the English name from Overture's names.common map. Was dropped at the
    # extract projection, not by upstream.
    name_en: str | None = None
    operating_status: str | None = None
    emails: tuple[str, ...] = ()
    socials: tuple[str, ...] = ()
    # v4 additions, matching what build_full_ptilesb.py actually writes.
    # SPEC.md's v4 section also lists star_rating, opening_hours and an
    # amenities block; the shipped encoder writes none of them, so they are
    # not modelled here.
    chain_count: int | None = None
    source_type: str | None = None
    source_id: str | None = None
    confidence: int | None = None


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
    # v1/v2 spend 0x10 on operating_status, so these records never carry an
    # English name. Defined so the shared return shape holds.
    name_en = None
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
        "name_en": name_en,
        "operating_status": operating_status,
        "emails": tuple(emails),
        "socials": tuple(socials),
    }, pos - offset


SOURCE_TYPES = {1: "overture", 2: "foursquare"}


def decode_business_record_v4(data: bytes, offset: int,
                              cell_center_micro: tuple[int, int]
                              ) -> tuple[dict, int]:
    """Decode one v4 business record. Returns (fields, bytes_consumed).

    Mirrors ``encode_v4`` in scripts/build_full_ptilesb.py, which is the only
    thing that has ever written these files. Three departures from SPEC.md's
    v4 section, which describes a format that was never built:

    * no u32 record_len prefix — records run back to back and the block is
      walked ``feature_count`` times, so a bad record desyncs the rest of the
      block rather than costing just itself;
    * the id is a sequential per-file counter, zigzag varint, not a delta and
      not a content hash;
    * coordinates are i16 offsets from the H3 cell centre, not absolute i32.

    The encoder falls back to i32 offsets when either exceeds i16 range and
    records no flag saying so, which is undecodable in principle. In practice
    it cannot happen: the offset is measured against the centre of the cell the
    point itself hashes to, and a res-7 cell spans ~0.02 deg against the i16
    ceiling of 0.32768 deg. This decoder therefore always reads i16.
    """
    pos = offset

    raw, consumed = decode_varint(data, pos)
    pos += consumed
    uid = zigzag_decode(raw)

    offset_lon, offset_lat = struct.unpack_from("<hh", data, pos)
    pos += 4
    lon_micro = cell_center_micro[0] + offset_lon
    lat_micro = cell_center_micro[1] + offset_lat

    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    category_idx = data[pos]
    pos += 1
    flags = data[pos]
    pos += 1

    phone = website = address = brand = None
    name_en = None
    chain_count = None

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
        # v5: name:en, written after brand and before chain_count. These records
        # carry no length prefix, so field order is the contract. A v4 file has
        # the bit clear, so no version check is needed. Note 0x10 meant
        # operating_status in v1/v2 -- v4 dropped that, which is what freed it.
        name_en, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x80:
        chain_count = data[pos]
        pos += 1

    # The extended word is written only when at least one of its bits is set,
    # and nothing marks its absence. Every record the builder emits carries a
    # non-zero source_type, so in the shipped files it is always present —
    # decode_block cross-checks the record count against the index to catch it
    # if that ever stops being true.
    source_type = source_id = confidence = None
    if pos + 2 <= len(data):
        ext_flags = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if ext_flags & 0x01:
            source_type = SOURCE_TYPES.get(data[pos], "unknown")
            pos += 1
        if ext_flags & 0x02:
            source_id, consumed = decode_string_u16(data, pos)
            pos += consumed
        if ext_flags & 0x04:
            confidence = data[pos]
            pos += 1

    return {
        "osm_id": uid,
        "lon": lon_micro / 100_000,
        "lat": lat_micro / 100_000,
        "name": name,
        "category_idx": category_idx,
        "phone": phone,
        "website": website,
        "address": address,
        "brand": brand,
        # v4 dropped the operating-status bits; everything reads as open.
        "operating_status": "open",
        "emails": (),
        "socials": (),
        "chain_count": chain_count,
        "source_type": source_type,
        "source_id": source_id,
        "confidence": confidence,
    }, pos - offset


def decode_block_v4(data: bytes, cell_center_micro: tuple[int, int],
                    feature_count: int) -> list[dict]:
    """Decode a v4 block: `feature_count` back-to-back records, no prefixes."""
    businesses: list[dict] = []
    pos = 0
    for i in range(feature_count):
        if pos >= len(data):
            break
        try:
            biz, consumed = decode_business_record_v4(
                data, pos, cell_center_micro,
            )
        except Exception as e:
            logger.warning(
                "v4 business record %d of %d desynced at byte %d: %s",
                i, feature_count, pos, e,
            )
            break
        businesses.append(biz)
        pos += consumed
    return businesses


def decode_block(data: bytes) -> list[dict]:
    """Decode a v1 block: { u32 record_len + record_body }*."""
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


class BusinessReader(BoundaryMixin):
    """Reader for .business.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str,
                 categories: list[str] | None = None):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)

        # Load zstd dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Read the spatial index. Entry width is measured from the section, not
        # inferred from the record version — the shipped v4 files pair v4
        # records with a 19-byte v1 index, so version >= 2 is not the same
        # question as "wide index entries".
        f.seek(self._header["index_offset"])
        index_bytes = f.read(self._header["index_length"])
        self._index, stride = read_index_auto(index_bytes)
        self._is_v2 = stride == INDEX_ENTRY_SIZE_V2

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
        self._cell_center_cache: dict[int, tuple[int, int]] = {}

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
        """Load categories from the sidecar JSON alongside the data file.

        The published files are named ``TX.business_v4.ptiles`` but their
        sidecar is ``TX.business_categories.json`` — the version suffix is on
        one and not the other — so try the unversioned name too. Without this
        every category resolves to None and category filters silently match
        nothing.
        """
        base = self._filepath
        if base.endswith(".ptiles"):
            base = base[:-7]
        candidates = [base + "_categories.json"]
        stripped = re.sub(r"_v\d+$", "", base)
        if stripped != base:
            candidates.append(stripped + "_categories.json")

        for sidecar_path in candidates:
            if not os.path.exists(sidecar_path):
                continue
            try:
                with open(sidecar_path) as f:
                    data = json.load(f)
                return data.get("categories", data if isinstance(data, list) else [])
            except Exception as e:
                logger.warning("Failed to load categories sidecar %s: %s",
                               sidecar_path, e)
        logger.warning("No categories sidecar found for %s (tried %s); "
                       "categories will be None",
                       self._filepath, ", ".join(candidates))
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

    def _cell_center_micro(self, cell_int: int) -> tuple[int, int]:
        """H3 cell centre in microdegrees, cached — v4 coords are relative."""
        cached = self._cell_center_cache.get(cell_int)
        if cached is None:
            lat, lon = h3.cell_to_latlng(format(cell_int, "x"))
            cached = (round(lon * 100_000), round(lat * 100_000))
            self._cell_center_cache[cell_int] = cached
        return cached

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
            if self._header["version"] >= 4:
                result = decode_block_v4(
                    raw, self._cell_center_micro(cell_int),
                    entry["feature_count"],
                )
            else:
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
            name_en=d.get("name_en"),
            operating_status=d.get("operating_status", "open"),
            emails=d.get("emails", ()),
            socials=d.get("socials", ()),
            chain_count=d.get("chain_count"),
            source_type=d.get("source_type"),
            source_id=d.get("source_id"),
            confidence=d.get("confidence"),
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
        """Find businesses near a point, nearest first.

        Spirals outward a ring at a time and stops as soon as the answer is
        settled. The previous implementation sized a grid_disk from
        radius_meters and read every cell in it unconditionally, so a 500 m
        lookup downtown decompressed seven blocks to answer from the first one.
        """
        from ptiles.nearest import nearest as _nearest

        def predicate(biz: Business) -> bool:
            if exclude_closed and biz.operating_status == "closed":
                return False
            if category_prefix is not None:
                if biz.category is None or not biz.category.startswith(category_prefix):
                    return False
            return True

        result = _nearest(
            self, lat, lon,
            predicate=predicate,
            n=limit,
            max_meters=radius_meters,
        )
        return [BusinessHit(business=h.feature, distance_meters=h.distance_meters)
                for h in result]

    _haversine = staticmethod(haversine_meters)

    def close(self) -> None:
        self._file.close()
