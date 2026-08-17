"""Scope naming: which country a scope belongs to, and where its file is published.

A *scope* is the prefix of a .ptiles filename -- the area one file covers.
Three shapes exist:

    TN              a US state (the original set; 51 of these)
    JP              a whole country, when its layers fit one file
    JP-KANTO        a subdivision, when they do not

Japan needs both at once: its buildings are eight regional files because 29.5M
buildings will not fit one build, while its other layers are country-wide. So
country and scope cannot be the same string, and a bare two-letter scope cannot
simply be read as a country -- `TN` is a state, `JP` is a country.

Published layout keeps the US at the snapshot root, because the live map and the
external JS client already read `maps/{date}/{ST}.{layer}.ptiles` and moving
those objects would break consumers outside this repo. Everything else gets a
country directory:

    maps/2026-08-20/TN.buildings_v9.ptiles
    maps/2026-08-20/JP/JP-KANTO.buildings_v9.ptiles

Both the manifest writer and the uploader import from here, so they cannot
disagree about where a file lives.
"""

import re

US = "US"

# A scope is a country/state code, optionally followed by a subdivision. The
# head may be two or three letters: ISO alpha-3 exists precisely so a country
# whose alpha-2 is taken can still be named (see collides_with_us_state).
SCOPE_RE = re.compile(r"^(?P<head>[A-Z]{2,3})(?:-(?P<sub>[A-Z0-9]+))?$")

# A bare two-letter scope is ambiguous on its own: `TN` is a US state, `JP` is a
# country. The US set was published first and owns the unprefixed namespace, so
# membership here decides it. Fixed data -- 50 states plus DC.
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


def collides_with_us_state(alpha2: str) -> bool:
    """Whether a country's ISO alpha-2 code is already a US state abbreviation.

    26 real countries collide -- CA is Canada and California, TN is Tunisia and
    Tennessee, DE is Germany and Delaware, and so on. Such a country cannot use
    a bare scope, because `CA.places_v1.ptiles` for Canada is the same filename
    at the same published path as California's. Use the alpha-3 code (`CAN`) or
    subdivision scopes (`CA-ON`) instead.

    This is a test against the state list rather than a list of countries: an
    enumerated collision list is exactly the kind of thing that goes stale, and
    the first version of it here was wrong -- it named 10 of the 26.
    """
    return alpha2.upper() in US_STATES


def country_of(scope: str) -> str:
    """Country a scope belongs to.

    `TN` -> `US` (a state), `US` -> `US`, `JP` -> `JP` (a country),
    `JP-KANTO` -> `JP` (a subdivision names its country first),
    `CAN` -> `CAN` (alpha-3, used where alpha-2 is taken by a state).
    """
    m = SCOPE_RE.match(scope)
    if not m:
        raise ValueError(f"not a scope: {scope!r}")
    head, sub = m["head"], m["sub"]
    if sub:
        # Explicitly qualified, so no ambiguity even for a colliding code.
        return head
    if len(head) == 3:
        # Alpha-3 is never a US state abbreviation, so it is always a country.
        return head
    return US if head in US_STATES or head == US else head


def check_country_scope(scope: str) -> None:
    """Raise if `scope` cannot name a non-US region unambiguously.

    Called when declaring a region, so a colliding code is caught at the table
    rather than by silently overwriting a US state's file in the bucket.
    """
    country = country_of(scope)
    if country == US:
        raise ValueError(
            f"scope {scope!r} resolves to the US: {scope.split('-')[0]!r} is a US "
            f"state abbreviation. Use the ISO alpha-3 code or a subdivision scope."
        )


def is_us(scope: str) -> bool:
    return country_of(scope) == US


def publish_relpath(scope: str, filename: str) -> str:
    """Path of a file within a published snapshot, relative to the date prefix.

    US files stay at the root so existing published URLs keep resolving; every
    other country gets its own directory.
    """
    country = country_of(scope)
    return filename if country == US else f"{country}/{filename}"


def scope_of_filename(filename: str) -> str:
    """Scope prefix of a .ptiles filename, e.g. `JP-KANTO.buildings_v9.ptiles`."""
    return filename.split(".", 1)[0]


def demo() -> None:
    assert country_of("TN") == "US"
    assert country_of("US") == "US"
    assert country_of("JP") == "JP"
    assert country_of("JP-KANTO") == "JP"
    assert country_of("JP-13") == "JP"
    assert is_us("TN") and not is_us("JP-KANTO")

    # A colliding code stays with the US when bare, and follows its own country
    # once qualified or written as alpha-3.
    assert country_of("DE") == "US"  # Delaware, not Germany
    assert country_of("DE-BY") == "DE"  # Bavaria
    assert country_of("DEU") == "DEU"  # Germany, unambiguously
    assert country_of("CAN-ON") == "CAN"
    assert collides_with_us_state("CA") and collides_with_us_state("TN")
    assert not collides_with_us_state("JP")

    # Declaring a region under a colliding bare code is refused outright.
    for bad in ("CA", "TN", "DE"):
        try:
            check_country_scope(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should be refused as a country scope")
    for good in ("JP", "JP-KANTO", "CAN", "CA-ON"):
        check_country_scope(good)

    for bad in ("", "j", "jp", "JAPN", "JP_KANTO", "JP-kanto", "TN.roads"):
        try:
            country_of(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected {bad!r} to be rejected")

    # US at the snapshot root, everyone else under a country directory.
    assert publish_relpath("TN", "TN.buildings_v9.ptiles") == "TN.buildings_v9.ptiles"
    assert publish_relpath("US", "US.admin.ptiles") == "US.admin.ptiles"
    assert publish_relpath("JP", "JP.places_v1.ptiles") == "JP/JP.places_v1.ptiles"
    assert (
        publish_relpath("JP-KANTO", "JP-KANTO.buildings_v9.ptiles")
        == "JP/JP-KANTO.buildings_v9.ptiles"
    )

    assert scope_of_filename("JP-KANTO.buildings_v9.ptiles") == "JP-KANTO"
    assert scope_of_filename("TN.roads.ptiles") == "TN"
    print("scopes: ok")


if __name__ == "__main__":
    demo()
