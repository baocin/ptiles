"""Records that are an airport's internal plumbing rather than places.

An OSM-derived business layer carries a departure board. Around Nashville and
Memphis the ground is thick with nodes named ``AA 1445 BNA-LAX``,
``DL 1656 - BNA to DTW``, ``Delta Flight 973 - MCI to ATL``, ``Gate B12``,
``Concourse C4``. None of them is somewhere a person goes: a flight is an event
and a gate is a doorway inside a building you have already arrived at. They
crowd out the real businesses near an airport and are never what a search for
one meant.

This is the build-side half of the rule, applied by
``scripts/build_full_ptilesb.py`` so new packs never carry these records at
all. The read-side half lives in ``core/src/flight_nodes.rs`` in the client, so
packs already on disk come back clean without being rebuilt. **The two have to
agree** -- they share the fixtures below.

Measured against the published ``TN.business.ptiles`` (829,528 named records)
the name rules drop 1,234, clustering on BNA, MEM, TYS and CHA, which is the
evidence they catch nothing else. Proximity to an airport is deliberately not
used: no builder emits an aeroway layer, so there is nothing to measure
against, and 45 of the names caught are flights logged nowhere near one.

The category these records belong to is
``Travel and Transportation > Transport Hub > Airport > Plane``, confirmed
against a categories sidecar -- but see :func:`flight_categories` for why it is
found by detection rather than by that label.
"""

from __future__ import annotations

import re

# Airline designators that begin a flight number. A closed list, because the
# letters are the only thing separating `DL 1656` from `BAS 128`. Accepting any
# two or three capitals -- which an earlier client-side rule did -- deleted 174
# real places in Tennessee alone: `BAC 41`, `AMB 210`, `HWY 385`, `ACT 1`.
CARRIERS = (
    "AA|DL|UA|WN|AS|B6|F9|NK|G4|HA|SY|AC|WS|9E|OO|YX|MQ|QX|ZW|EV|YV"
)

# `DL3208`, `AA 1087`, `UA6157 To DEN`, `AA 2926 CHA/DFW Seat 11A`. Whatever
# follows the number is ignored rather than described: it is free text -- a
# route, a seat, a note -- and the designator alone is decisive. The boundary
# after the digits is not ignored, so `AA 12Th Street Diner` does not match.
_DESIGNATOR = re.compile(rf"^(?:{CARRIERS})\s?\d{{1,4}}[A-Za-z]?(?:\b.*)?$")

# `Delta Flight 973 - MCI to ATL`. An airline must precede it (or this is
# `Flight 93 Memorial`) and a number must follow (or this is `Flight Deck Bar`).
_SPELLED = re.compile(
    r"\b(?:Delta|American|United|Southwest|Alaska|JetBlue|Frontier|Spirit"
    r"|Allegiant|Hawaiian|Envoy|Republic|SkyWest)\b[^\n]{0,40}?"
    r"\bFlight\s+\d+",
    re.IGNORECASE,
)

# `Gate 5`, `Gate B12`, `Terminal 2`, `Concourse C4`. The whole name must be the
# word and its number, which is what keeps `Gate Communications` and `Gateway
# Tire` -- both real businesses -- out of it.
_AIRSIDE = re.compile(
    r"^(?:gate|concourse|stand|apron|terminal|pier)\s*[-#]?\s*[A-Za-z]?\d{1,3}[A-Za-z]?$",
    re.IGNORECASE,
)


# A category is a flight bucket when most of what it holds is named like a
# flight. Below this share it is left alone.
#
# The categories are the decisive signal and the names are only how the
# categories are found. In the Tennessee pack one category holds 1,710 records,
# 922 of them named like flights (54%); the remaining 788 are the same thing
# written in ways no pattern catches -- `Im On A Plane`, `Seat 3C In First
# Class`, `Flight To Des Moines`, `First Class`, `The Brink Of Destruction`.
# Dropping the category takes all 1,710.
#
# Detected rather than named, for three reasons. Guessing label text goes
# wrong ("Flight School" is a business people drive to). The index is per
# state: `build_full_ptilesb.py` numbers categories by frequency rank within
# each state (`cat_idx = {c: i + 1 for i, (c, _) in enumerate(sorted_cats[:254])}`).
# And the numbering moves between builds of the *same* state -- the flight
# category is index 94 in the published TN.business.ptiles and index 96 in a
# TN categories sidecar built from 980,499 places rather than 829,528, with
# nothing in either file recording which build the other came from. A consumer
# pairing the two would label every flight an elementary school.
#
# It is also why the filter cannot run on the phone: the client sees an index,
# never a label.
FLIGHT_CATEGORY_SHARE = 0.4

# Below this, a category is too small for its share to mean anything: three
# records of which two are `AA 100` says nothing about the category.
FLIGHT_CATEGORY_MIN = 50


def flight_categories(records) -> set[str]:
    """Category labels whose records are overwhelmingly flights.

    `records` is any iterable of dicts with `primary_category` and `name`.
    """
    from collections import Counter

    total: Counter[str] = Counter()
    flights: Counter[str] = Counter()
    for rec in records:
        category = rec.get("primary_category") or ""
        if not category:
            continue
        total[category] += 1
        if is_flight_node(rec.get("name")):
            flights[category] += 1
    return {
        category
        for category, count in total.items()
        if count >= FLIGHT_CATEGORY_MIN
        and flights[category] / count >= FLIGHT_CATEGORY_SHARE
    }


def is_flight_node(name: str | None) -> bool:
    """True when a name says the record is a flight or a gate, not a place."""
    if not name:
        return False
    trimmed = name.strip()
    if not trimmed:
        return False
    return bool(
        _DESIGNATOR.match(trimmed)
        or _SPELLED.search(trimmed)
        or _AIRSIDE.match(trimmed)
    )


# Names read out of the published TN.business.ptiles, shared with the Rust
# implementation's tests so the two rules cannot drift apart silently.
DROP_FIXTURES = [
    "DL3208",
    "AA 1087",
    "AA 1445 BNA-LAX",
    "DL 1656 - BNA to DTW",
    "UA6157 To DEN",
    "AA3908 BNA - ORD",
    "AA 2903 CHA/DFW",
    "AA 2999 (TYS > ORD)",
    "AA 2926 CHA/DFW Seat 11A",
    "AA 1735 MEM to DFW Non-stop",
    "Delta Flight 973 - MCI to ATL",
    "American Airlines Flight 1221",
    "delta flight 2323",
    "Gate 5",
    "Gate B7",
    "Gate C20",
    "gate a3",
    "Terminal 2",
]

KEEP_FIXTURES = [
    "HWY 54",
    "HWY 385",
    "HWY 45N",
    "US 51",
    "TN0106",
    "BAC 41",
    "BAS 128",
    "AMB 210",
    "ACT 1",
    "ABC24",
    "FOX 16",
    "OR 7",
    "PT2",
    "KU4K",
    "HWY 55 Burgers",
    "US 43 Drag Raceway",
    "ONE9 Travel Center",
    "VFW 4840 - Ray Pinner Post, Tipton County, TN",
    "Gate Communications",
    "Gateway Tire",
    "Golden Gate Cafe",
    "Flight 93 Memorial",
    "Delta Dental of Tennessee",
    "United Grocery Outlet",
]


def _self_check() -> None:
    for name in DROP_FIXTURES:
        assert is_flight_node(name), f"should drop: {name!r}"
    for name in KEEP_FIXTURES:
        assert not is_flight_node(name), f"should keep: {name!r}"
    print(f"ok: {len(DROP_FIXTURES)} dropped, {len(KEEP_FIXTURES)} kept")


if __name__ == "__main__":
    _self_check()
