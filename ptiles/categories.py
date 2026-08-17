"""One vocabulary for business categories, and stable ids for it.

The `primary_category` field arrives from two sources and holds two different
vocabularies. Of the 254 categories in the published Tennessee pack, 157 are
Overture paths (``Community and Government > Spiritual Center > Church``) and
97 are bare labels, themselves of two kinds: snake_case leaves
(``church_cathedral``, ``gas_station``) and bare top-level group names
(``Retail``, ``Arts and Entertainment``) where the source gave nothing finer.
Fourteen bare labels are the same thing as a path leaf spelled differently --
``farm``, ``park``, ``hotel``, ``restaurant``, ``gym``, ``dentist``.

So "make the category ids consistent" cannot start with the ids. Two packs
cannot agree on a number for ``Church`` while one of them calls it
``church_cathedral``.

This module is the vocabulary layer:

- :func:`canonical` reduces either spelling to one snake_case leaf.
- :func:`group_of` gives the coarse family a leaf belongs to, which is the
  part that is worth putting in a byte and comparing across packs.
There is deliberately no global id registry. An earlier version kept one --
an append-only ``category_ids.json`` mapping every leaf to a number that never
moved -- and it was redundant the moment each pack began carrying its own
table: what crosses between packs is the *label* and the *group*, both of
which travel inside the file. A second numbering would only be one more thing
to keep in step.
"""

from __future__ import annotations

import re

# The coarse families, in the source's own words. Anything whose path starts
# with one of these belongs to it; everything else is placed by
# `_LEAF_GROUPS` or falls back to `other`.
GROUPS = (
    "arts_and_entertainment",
    "business_and_professional_services",
    "community_and_government",
    "dining_and_drinking",
    "health_and_medicine",
    "landmarks_and_outdoors",
    "retail",
    "sports_and_recreation",
    "travel_and_transportation",
    "other",
)

_PUNCT = re.compile(r"[^a-z0-9]+")

# The bare vocabulary, placed by hand.
#
# These labels arrive with no ancestry at all, so nothing in the data says
# where they belong -- `gas_station` and `baptist_church` are just words. Every
# one of them is an ordinary kind of business, and there are 57, so the honest
# way to place them is to place them. Anything not here and not a path is
# `other`, which stays visible rather than being guessed at.
_BARE_GROUPS = {
    # Money, trades, offices and services sold to the public.
    "atms": "business_and_professional_services",
    "automotive_repair": "business_and_professional_services",
    "bank_credit_union": "business_and_professional_services",
    "banks": "business_and_professional_services",
    "barber": "business_and_professional_services",
    "beauty_and_spa": "business_and_professional_services",
    "beauty_salon": "business_and_professional_services",
    "construction_services": "business_and_professional_services",
    "contractor": "business_and_professional_services",
    "event_planning": "business_and_professional_services",
    "freight_and_cargo_service": "business_and_professional_services",
    "hvac_services": "business_and_professional_services",
    "industrial_equipment": "business_and_professional_services",
    "landscaping": "business_and_professional_services",
    "lawyer": "business_and_professional_services",
    "money_transfer_services": "business_and_professional_services",
    "printing_services": "business_and_professional_services",
    "professional_services": "business_and_professional_services",
    "propane_supplier": "business_and_professional_services",
    "real_estate": "business_and_professional_services",
    "real_estate_agent": "business_and_professional_services",
    "roofing": "business_and_professional_services",
    "self_storage_facility": "business_and_professional_services",
    "tattoo_and_piercing": "business_and_professional_services",
    "tire_dealer_and_repair": "business_and_professional_services",
    # Shops. A car dealer sells cars; a repair shop is a service above.
    "automotive_parts_and_accessories": "retail",
    "building_supply_store": "retail",
    "car_dealer": "retail",
    "flowers_and_gifts_shop": "retail",
    "furniture_store": "retail",
    "shopping": "retail",
    "thrift_store": "retail",
    "used_car_dealer": "retail",
    "womens_clothing_store": "retail",
    # Worship, schooling, civic life. Spiritual centres sit here in the path
    # vocabulary too (`Community and Government > Spiritual Center > Church`).
    "baptist_church": "community_and_government",
    "church_cathedral": "community_and_government",
    "college_university": "community_and_government",
    "community_services_non_profits": "community_and_government",
    "fire_department": "community_and_government",
    "public_and_government_association": "community_and_government",
    "public_service_and_government": "community_and_government",
    "religious_organization": "community_and_government",
    "retirement_home": "community_and_government",
    "school": "community_and_government",
    # Care.
    "counseling_and_mental_health": "health_and_medicine",
    "doctor": "health_and_medicine",
    "family_practice": "health_and_medicine",
    "health_and_medical": "health_and_medicine",
    "home_health_care": "health_and_medicine",
    "physical_therapy": "health_and_medicine",
    # Getting somewhere, and staying there. Fuel is transport infrastructure
    # rather than a shop, which is the choice most likely to be argued with.
    "cabin": "travel_and_transportation",
    "gas_station": "travel_and_transportation",
    "holiday_rental_home": "travel_and_transportation",
    "truck_rentals": "travel_and_transportation",
    # Places rather than businesses.
    "landmark_and_historical_building": "landmarks_and_outdoors",
    "mountain": "landmarks_and_outdoors",
    "pizza_restaurant": "dining_and_drinking",
}


def canonical(label: str | None) -> str:
    """One snake_case leaf for either vocabulary.

    A path keeps only its last element, because that is what the bare labels
    are: ``Community and Government > Spiritual Center > Church`` and
    ``church`` describe the same place, and the ancestry is recoverable from
    :func:`group_of`.
    """
    if not label:
        return ""
    leaf = label.split(">")[-1].strip().lower()
    return _PUNCT.sub("_", leaf).strip("_")


def learn_groups(labels) -> dict[str, str]:
    """Leaf to family, read off the paths in one build's own vocabulary.

    A path states its family; a bare label does not. Where the same state
    holds both spellings -- ``Landmarks and Outdoors > Park`` and ``park`` --
    the path teaches the bare one, and nothing has to be remembered between
    builds to do it.
    """
    learned: dict[str, str] = {}
    for label in labels or ():
        if ">" not in (label or ""):
            continue
        root = canonical(label.split(">")[0])
        if root in GROUPS:
            learned[canonical(label)] = root
    return learned


def group_of(label: str | None, learned: dict[str, str] | None = None) -> str:
    """The coarse family of a category, from its path or from what it is.

    `learned` is what :func:`learn_groups` read off the paths in the same
    build, so a bare label can inherit the family of the path that names the
    same leaf. Anything still unplaced is ``other``, which is a fact about the
    input rather than a category.
    """
    if not label:
        return "other"
    if ">" in label:
        root = canonical(label.split(">")[0])
        return root if root in GROUPS else "other"
    leaf = canonical(label)
    # A source that gave only the family name, e.g. `Retail`.
    if leaf in GROUPS:
        return leaf
    if leaf in _BARE_GROUPS:
        return _BARE_GROUPS[leaf]
    return (learned or {}).get(leaf, "other")


def _self_check() -> None:
    assert canonical("Community and Government > Spiritual Center > Church") == "church"
    assert canonical("church_cathedral") == "church_cathedral"
    assert canonical("Doctor's Office") == "doctor_s_office"
    assert canonical(None) == "" and canonical("") == ""

    # Both spellings of the same idea land on one leaf.
    assert canonical("Landmarks and Outdoors > Park") == canonical("park") == "park"

    # A path places itself; a bare family name is its own group.
    assert group_of("Retail > Grocery Store") == "retail"
    assert group_of("Retail") == "retail"
    assert group_of("Dining and Drinking > Restaurant") == "dining_and_drinking"
    assert group_of("something nobody has seen") == "other"

    # A path in the same build teaches the bare spelling its family.
    learned = learn_groups(["Landmarks and Outdoors > Park", "Retail > Grocery Store"])
    assert group_of("park", learned) == "landmarks_and_outdoors"
    assert group_of("park") == "other", "with nothing learned, nothing is guessed"

    print(f"ok: {len(_BARE_GROUPS)} bare labels placed by hand, groups {len(GROUPS)}")


if __name__ == "__main__":
    _self_check()
