"""One place, one record.

The business layer is built by merging Foursquare and Overture with no dedupe
pass, so most real places are in it twice under slightly different spellings
and coordinates. Measured against the published ``TN.business.ptiles``: 50,037
records share a name *and* a four-decimal coordinate with another (6.0%), and
102,172 pairs share a name within 150 m (~12% of the layer).

It is visible everywhere the layer is read. A search for a marina lists
``Cherokee Boat Dock`` three times at 979 ft, 979 ft and 999 ft; a search near
Jackson returns ``Dudley's Recycling`` and ``Dudleys Recycling Inc`` as two
answers. Ranking cannot fix it, because both really are equally good matches --
they are the same place.

Merging rather than dropping: the duplicate usually carries a field the
survivor lacks (one source has the phone, the other the website), so the kept
record is filled from the ones it absorbs. Nothing is lost but the row.
"""

from __future__ import annotations

import re
import unicodedata

# How near two records must be to be the same place, in degrees. 0.0005 lat is
# about 55 m; longitude is divided by the cosine of the latitude in
# `_cell`, so the cell is roughly square on the ground.
#
# Deliberately tighter than the 150 m the audit measured. Two units of a strip
# mall sit 30 m apart and are different businesses; the same import twice is
# usually within a few metres. Merging is not reversible, so the rule errs
# towards leaving a pair alone.
CELL_DEG = 0.0005

# Words that do not distinguish one business from another when everything else
# about the record agrees.
_NOISE = re.compile(
    r"\b(?:inc|inc\.|llc|l\.l\.c|ltd|co|co\.|corp|corporation|company|the)\b",
    re.IGNORECASE,
)
# Apostrophes vanish rather than becoming spaces: `Dudley's` and `Dudleys` are
# the same word, and splitting the first into `dudley s` would keep them apart.
_APOSTROPHE = re.compile(r"['\u2019\u02bc]")
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def fold(name: str) -> str:
    """A name reduced to what distinguishes it.

    Accent-fold and lowercase as `build_business_name_index._fold_name` does,
    then drop punctuation and corporate noise, so ``Dudley's Recycling`` and
    ``Dudleys Recycling Inc`` land on the same string. The trailing words are
    removed rather than kept because a source that writes ``Inc`` and one that
    does not are describing the same firm.
    """
    decomposed = unicodedata.normalize("NFD", name or "")
    stripped = "".join(c for c in decomposed if not (0x0300 <= ord(c) <= 0x036F))
    lowered = stripped.lower().replace("ß", "ss")
    without_punct = _PUNCT.sub(" ", _APOSTROPHE.sub("", lowered))
    without_noise = _NOISE.sub(" ", without_punct)
    return _SPACE.sub(" ", without_noise).strip()


def _cell(lat: float, lon: float) -> tuple[int, int]:
    """The coordinate rounded to a roughly square cell of `CELL_DEG` latitude."""
    import math

    shrink = max(math.cos(math.radians(lat)), 0.05)
    return (round(lat / CELL_DEG), round(lon * shrink / CELL_DEG))


def _completeness(rec: dict) -> tuple:
    """How good a survivor a record is: confident first, then filled in.

    Confidence comes from the source and is the only opinion either source
    offers about its own record. After that, the record that carries more of
    an address, a phone and a website is the better one to keep, and the name
    breaks the final tie so the choice does not depend on input order.
    """
    filled = sum(
        1 for key in ("address", "city", "phone", "website", "brand") if rec.get(key)
    )
    return (rec.get("confidence") or 0, filled, rec.get("name") or "")


# Fields taken from a duplicate when the survivor's own is empty.
MERGEABLE = ("address", "city", "phone", "website", "brand", "primary_category")


def dedupe(records: list[dict]) -> tuple[list[dict], int]:
    """Collapse records that name the same place at the same spot.

    Returns the kept records and how many were absorbed. Order is preserved
    for what survives, so a rebuilt pack differs from its predecessor only by
    the rows that were duplicates.

    The eight neighbouring cells are probed as well as the record's own, or a
    pair either side of a cell boundary would survive as two -- which is most
    of the point, since the coordinates that differ slightly are exactly the
    ones that land in different cells.
    """
    index: dict[tuple[str, int, int], int] = {}
    kept: list[dict] = []
    absorbed = 0
    for rec in records:
        name = fold(rec.get("name") or "")
        if not name:
            kept.append(rec)
            continue
        y, x = _cell(rec["lat"], rec["lon"])
        found = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                at = index.get((name, y + dy, x + dx))
                if at is not None:
                    found = at
                    break
            if found is not None:
                break
        if found is None:
            index[(name, y, x)] = len(kept)
            kept.append(rec)
            continue
        absorbed += 1
        winner, loser = kept[found], rec
        if _completeness(loser) > _completeness(winner):
            winner, loser = loser, winner
        for field in MERGEABLE:
            if not winner.get(field) and loser.get(field):
                winner[field] = loser[field]
        kept[found] = winner
    return kept, absorbed


def _self_check() -> None:
    same_place = [
        {"name": "Dudley's Recycling", "lat": 35.6, "lon": -88.8, "phone": "731-555-0100",
         "confidence": 80},
        {"name": "Dudleys Recycling Inc", "lat": 35.60002, "lon": -88.80002,
         "website": "example.com", "confidence": 90},
    ]
    kept, absorbed = dedupe(same_place)
    assert absorbed == 1, kept
    assert kept[0]["phone"] == "731-555-0100", "the phone must survive the merge"
    assert kept[0]["website"] == "example.com", "and so must the website"

    # Two units of a strip mall, 300 m apart, are two businesses.
    neighbours = [
        {"name": "Kroger", "lat": 35.6, "lon": -88.8},
        {"name": "Kroger", "lat": 35.603, "lon": -88.8},
    ]
    assert dedupe(neighbours)[1] == 0, "300 m apart is not the same shop"

    # A pair either side of a cell boundary still merges.
    boundary = [
        {"name": "Waffle House", "lat": 35.60024, "lon": -88.8},
        {"name": "Waffle House", "lat": 35.60026, "lon": -88.8},
    ]
    assert dedupe(boundary)[1] == 1, "a cell edge must not hide a duplicate"

    # Different names at one spot are left alone.
    shared = [
        {"name": "Shell", "lat": 35.6, "lon": -88.8},
        {"name": "Subway", "lat": 35.6, "lon": -88.8},
    ]
    assert dedupe(shared)[1] == 0, "a fuel station and the sandwich shop inside it"

    print("ok: merges duplicates, keeps neighbours, spans cell boundaries")


if __name__ == "__main__":
    _self_check()
