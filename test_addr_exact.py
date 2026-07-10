"""Test the exact AddrExtractor from the build script."""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/kino/projects/ptiles/scripts"))

from pathlib import Path
import osmium
import h3

PBF_MAP = {"TN": "tennessee"}
PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
OUTPUT_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))


class AddrExtractor(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.addrs = []

    def way(self, w):
        hn = st = None
        has_building = False
        for t in w.tags:
            if t.k == "building" and t.v:
                has_building = True
            elif t.k == "addr:housenumber" and t.v:
                hn = t.v
            elif t.k == "addr:street" and t.v:
                st = t.v
        if not has_building or not hn:
            return
        if not w.nodes:
            return
        try:
            n = next(w.nodes)
            lon, lat = n.lon, n.lat
        except:
            return
        try:
            cell = h3.latlng_to_cell(lat, lon, 7)
        except:
            return
        self.addrs.append(
            {
                "osm_id": w.id,
                "lon": lon,
                "lat": lat,
                "housenumber": hn,
                "street": st or "",
                "cell": int(cell, 16) if isinstance(cell, str) else cell,
            }
        )


h = AddrExtractor()
h.apply_file(str(PBF_DIR / "tennessee-latest.osm.pbf"), locations=True)
print(f"Extracted {len(h.addrs)} addresses with addr:housenumber")
if h.addrs:
    print(f"First: {h.addrs[0]}")
