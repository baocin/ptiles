#!/usr/bin/env python3
"""Test: find cell near Nashville in business ptiles and decompress."""

import struct
import h3
import sys
import zstandard as zstd

sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

FP = "/home/aoi/kino/projects/ptiles/data/states/TN.business.ptiles"

with open(FP, "rb") as f:
    hdr = f.read(256)

pos = 0
magic = hdr[0:7].decode()
pos += 8
ver = hdr[8]
pos = 40  # skip to dict_offset
doff = struct.unpack_from("<Q", hdr, 40)[0]
dlen = struct.unpack_from("<I", hdr, 48)[0]
ioff = struct.unpack_from("<Q", hdr, 52)[0]
ilen = struct.unpack_from("<I", hdr, 60)[0]
boff = struct.unpack_from("<Q", hdr, 64)[0]

print(f"dict_offset={doff}, dlen={dlen}", flush=True)
print(f"index_offset={ioff}, ilen={ilen}", flush=True)
print(f"blocks_offset={boff}", flush=True)

with open(FP, "rb") as f:
    f.seek(ioff)
    idx = f.read(ilen)
    f.seek(doff)
    dict_data = f.read(dlen)

entry_count = struct.unpack_from("<I", idx, 0)[0]
print(f"Entries: {entry_count}", flush=True)

cells = {}
off = 4
for i in range(min(entry_count, 50000)):
    if off + 19 > len(idx):
        break
    h3_cell = struct.unpack_from("<Q", idx, off)[0]
    bo2 = int.from_bytes(idx[off + 8 : off + 14], "little")
    bl2 = int.from_bytes(idx[off + 14 : off + 17], "little")
    fc2 = struct.unpack_from("<H", idx, off + 17)[0]
    cells[h3_cell] = (bo2, bl2, fc2)
    off += 19

print(f"Cells loaded: {len(cells)}", flush=True)

lat, lon = 36.1605, -86.7765
cell = h3.latlng_to_cell(lat, lon, 7)
cell_int = int(cell, 16)

found = False
for k in range(4):
    if found:
        break
    ring = h3.grid_ring(cell, k)
    for c in ring:
        ci = int(c, 16)
        if ci in cells:
            bo2, bl2, fc2 = cells[ci]
            print(
                f"Found ring {k}: {c} offset={bo2}, len={bl2}, features={fc2}",
                flush=True,
            )

            with open(FP, "rb") as f:
                f.seek(boff + bo2)
                compressed = f.read(bl2)

            dctx = zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(dict_data))
            raw = dctx.decompress(compressed)
            print(f"  Decompressed: {len(raw)} bytes", flush=True)

            # String table
            str_count = raw[0]
            p = 1
            strings = []
            for i in range(str_count):
                slen = raw[p]
                p += 1
                strings.append(raw[p : p + slen].decode("utf-8", errors="replace"))
                p += slen
            print(f"  Strings: {strings[:3]}", flush=True)

            # Parse records
            prev_uid = 0
            recs = []
            while p + 4 <= len(raw):
                rl = struct.unpack_from("<I", raw, p)[0]
                p += 4
                if p + rl > len(raw):
                    break
                rec = raw[p : p + rl]
                rp = 0

                # uid delta (varint)
                dr_val = 0
                dr_shift = 0
                while rp < len(rec):
                    b = rec[rp]
                    rp += 1
                    dr_val |= (b & 0x7F) << dr_shift
                    dr_shift += 7
                    if not (b & 0x80):
                        break
                uid_delta = (dr_val >> 1) ^ -(dr_val & 1)
                uid = (prev_uid + uid_delta) & 0xFFFFFFFF
                prev_uid = uid

                if rp + 8 > len(rec):
                    break
                mlon = struct.unpack_from("<i", rec, rp)[0]
                rp += 4
                mlat = struct.unpack_from("<i", rec, rp)[0]
                rp += 4

                if rp + 2 > len(rec):
                    break
                nlen = struct.unpack_from("<H", rec, rp)[0]
                rp += 2
                if rp + nlen > len(rec):
                    break
                name = rec[rp : rp + nlen].decode("utf-8", errors="replace")
                rp += nlen

                if rp >= len(rec):
                    break
                cat_idx = rec[rp]
                rp += 1
                cat = (
                    strings[cat_idx - 1]
                    if cat_idx > 0 and cat_idx - 1 < len(strings)
                    else ""
                )

                # flags
                if rp >= len(rec):
                    break
                flags = rec[rp]
                rp += 1

                entry = {
                    "name": name,
                    "cat": cat,
                    "lon": mlon / 100000,
                    "lat": mlat / 100000,
                }

                # Optional fields
                if flags & 0x01 and rp < len(rec):  # phone
                    plen = rec[rp]
                    rp += 1
                    entry["phone"] = (
                        rec[rp : rp + plen].decode() if rp + plen <= len(rec) else ""
                    )
                    rp += plen
                if flags & 0x08 and rp < len(rec):  # brand
                    blen = rec[rp]
                    rp += 1
                    entry["brand"] = (
                        rec[rp : rp + blen].decode() if rp + blen <= len(rec) else ""
                    )
                    rp += blen
                if flags & 0x80 and rp < len(rec):  # chain count
                    entry["chain"] = rec[rp]
                    rp += 1

                recs.append(entry)
                if len(recs) >= 10:
                    break
                p += rl

            print(f"  Records: {len(recs)}", flush=True)
            for r in recs:
                brand_str = f" brand={r.get('brand', '')}" if r.get("brand") else ""
                chain_str = f" chain={r.get('chain', '')}" if r.get("chain") else ""
                print(
                    f"    {r['name'][:40]:40s}  ({r['lat']:.5f},{r['lon']:.5f}){brand_str}{chain_str}",
                    flush=True,
                )

            found = True
            break

if not found:
    print(f"Cell {cell} not in index, and no neighbor found in rings 1-3", flush=True)
