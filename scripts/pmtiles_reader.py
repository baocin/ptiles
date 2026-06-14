#!/usr/bin/env python3
"""
Custom PMTiles v3 reader for the Overture 2026-05-13 export.
Handles the non-standard header layout and uncompressed leaf directories.
"""

import io
import struct
import gzip
import mmap
import os
from collections import namedtuple


def read_varint(buf, pos):
    v = 0
    s = 0
    while pos < len(buf):
        b = buf[pos]
        v |= (b & 0x7F) << s
        s += 7
        pos += 1
        if not (b & 0x80):
            break
    return v, pos


class PMTilesReader:
    """Minimal PMTiles v3 reader for Overture exports."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.mapping = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        self._read_header()
        self._read_root()

    def _read_header(self):
        """Parse the 127-byte PMTiles v3 header."""
        h = self.mapping[:127]
        self.header = {
            "version": h[7],
            "root_offset": struct.unpack("<Q", h[8:16])[0],
            "root_length": struct.unpack("<Q", h[16:24])[0],
            "metadata_offset": struct.unpack("<Q", h[24:32])[0],
            "metadata_length": struct.unpack("<Q", h[32:40])[0],
            "leaf_directory_offset": struct.unpack("<Q", h[40:48])[0],
            "leaf_directory_length": struct.unpack("<Q", h[48:56])[0],
            "tile_data_offset": struct.unpack("<Q", h[56:64])[0],
            "tile_data_length": struct.unpack("<Q", h[64:72])[0],
            "addressed_tiles_count": struct.unpack("<Q", h[72:80])[0],
            "tile_entries_count": struct.unpack("<Q", h[80:88])[0],
            "tile_contents_count": struct.unpack("<Q", h[88:96])[0],
            "clustered": h[96] == 0x1,
            "internal_compression": h[97],
            "tile_compression": h[98],
            "tile_type": h[99],
            "min_zoom": h[100],
            "max_zoom": h[101],
            "min_lon_e7": struct.unpack("<i", h[102:106])[0],
            "min_lat_e7": struct.unpack("<i", h[106:110])[0],
            "max_lon_e7": struct.unpack("<i", h[110:114])[0],
            "max_lat_e7": struct.unpack("<i", h[114:118])[0],
            "center_zoom": h[118],
            "center_lon_e7": struct.unpack("<i", h[119:123])[0],
            "center_lat_e7": struct.unpack("<i", h[123:127])[0],
        }

    def _read_root(self):
        h = self.header
        raw = self.mapping[h["root_offset"]:h["root_offset"] + h["root_length"]]
        # Root directory is gzip-compressed
        self.root_dir = self._parse_dir(gzip.decompress(raw))

    def _parse_dir(self, data):
        """Parse a PMTiles directory (uncompressed varints)."""
        pos = 0
        num_entries, pos = read_varint(data, pos)

        entries = []
        last_id = 0
        for i in range(num_entries):
            delta, pos = read_varint(data, pos)
            last_id += delta
            entries.append({"tile_id": last_id})

        for i in range(num_entries):
            entries[i]["run_length"], pos = read_varint(data, pos)

        for i in range(num_entries):
            entries[i]["length"], pos = read_varint(data, pos)

        prev_offset = 0
        prev_length = 0
        for i in range(num_entries):
            tmp, pos = read_varint(data, pos)
            if i > 0 and tmp == 0:
                entries[i]["offset"] = prev_offset + prev_length
            else:
                entries[i]["offset"] = tmp - 1
            prev_offset = entries[i]["offset"]
            prev_length = entries[i]["length"]

        return entries

    def find_entry(self, tile_id):
        """Find the root directory entry that covers a tile_id."""
        entries = self.root_dir
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid]["tile_id"] <= tile_id:
                lo = mid + 1
            else:
                hi = mid
        idx = lo - 1
        if idx < 0:
            return None
        return entries[idx]

    def get_tile_data(self, z, x, y):
        """Get raw tile bytes for a given z/x/y coordinate."""
        tile_id = self._zxy_to_tileid(z, x, y)
        entry = self.find_entry(tile_id)
        if not entry:
            return None

        if entry["run_length"] > 0:
            # Direct tile data
            off = self.header["tile_data_offset"] + entry["offset"]
            return self.mapping[off:off + entry["length"]]

        # Leaf directory (run_length == 0)
        leaf_off = entry["offset"]  # Absolute offset in this file
        leaf_len = entry["length"]
        leaf_raw = self.mapping[leaf_off:leaf_off + leaf_len]
        
        # Leaf dirs are uncompressed varints
        if leaf_raw[0] == 0x1f and leaf_raw[1] == 0x8b:
            leaf_raw = gzip.decompress(leaf_raw)
            
        leaf_entries = self._parse_dir(leaf_raw)

        # Find tile in leaf
        lo, hi = 0, len(leaf_entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if leaf_entries[mid]["tile_id"] <= tile_id:
                lo = mid + 1
            else:
                hi = mid
        idx = lo - 1
        if idx < 0:
            return None

        le = leaf_entries[idx]

        # Check if tile_id is within the run_length of this entry
        if le["run_length"] > 0 and tile_id >= le["tile_id"] and tile_id < le["tile_id"] + le["run_length"]:
            # Calculate offset within the run
            tile_in_run = tile_id - le["tile_id"]
            tile_len = le["length"]
            tile_off = le["offset"] + tile_in_run * tile_len
            return self.mapping[self.header["tile_data_offset"] + tile_off: self.header["tile_data_offset"] + tile_off + tile_len]

        return None

    @staticmethod
    def _zxy_to_tileid(z, x, y):
        """Convert (z, x, y) to PMTiles tile ID."""
        return ((1 << (2 * z + 1)) - 1) // 3 + y * (1 << z) + x

    @staticmethod
    def _tileid_to_zxy(tile_id):
        """Convert PMTiles tile ID to (z, x, y)."""
        z = tile_id.bit_length() // 2
        offset = ((1 << (2 * z + 1)) - 1) // 3
        remainder = tile_id - offset
        y = remainder // (1 << z)
        x = remainder % (1 << z)
        return z, x, y

    def close(self):
        self.mapping.close()
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
