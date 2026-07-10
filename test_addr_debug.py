"""Debug: extract addresses from TN PBF."""

import sys

sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

from pathlib import Path
import osmium

pbfp = Path("/mnt/aoi/kino/ptiles/pbfs/tennessee-latest.osm.pbf")


class AddrExtractor(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways_seen = 0
        self.buildings_seen = 0
        self.addrs_seen = 0

    def way(self, w):
        self.ways_seen += 1
        hn = st = None
        has_building = False
        for t in w.tags:
            if t.k == "building" and t.v:
                has_building = True
            elif t.k == "addr:housenumber" and t.v:
                hn = t.v
        if has_building:
            self.buildings_seen += 1
        if has_building and hn:
            self.addrs_seen += 1
        if self.ways_seen % 500000 == 0:
            print(
                f"  ways={self.ways_seen} buildings={self.buildings_seen} addrs={self.addrs_seen}",
                flush=True,
            )


h = AddrExtractor()
h.apply_file(str(pbfp), locations=True)
print(
    f"Total: ways={h.ways_seen} buildings={h.buildings_seen} addr_buildings={h.addrs_seen}"
)
