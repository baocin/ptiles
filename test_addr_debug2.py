"""Debug: check if node coordinates work with location.valid()."""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/kino/projects/ptiles/scripts"))

from pathlib import Path
import osmium

pbfp = Path("/mnt/aoi/kino/ptiles/pbfs/tennessee-latest.osm.pbf")


class Test(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = 0
        self.valid_first = 0

    def way(self, w):
        self.ways += 1
        if self.ways > 500000:
            return
        if not w.nodes:
            return
        for n in w.nodes:
            if n.location.valid():
                self.valid_first += 1
                break


h = Test()
h.apply_file(str(pbfp), locations=True)
print(f"Ways: {h.ways}, first node valid: {h.valid_first}")
