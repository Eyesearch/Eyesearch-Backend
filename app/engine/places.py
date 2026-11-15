"""Indian states and union territories, and the domains that speak for them.

A query that names a place should be answered by that place. The index had no
way to know that "karnataka" in a question and `karnataka.gov.in` in a URL are
the same fact, so a Nilgiris (Tamil Nadu) page that mentioned the word once
answered a Karnataka question.

The table is data, not policy: every state and union territory, its official
portal, and the spellings people use. Nothing here is specific to one state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    name: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...] = ()


#: Every state and union territory with its government portal. The portal is the
#: signal: a page on karnataka.gov.in is a Karnataka answer in a way that a page
#: mentioning Karnataka is not.
PLACES: tuple[Place, ...] = (
    Place("andhra pradesh", ("ap.gov.in",), ("andhra", "ap")),
    Place("arunachal pradesh", ("arunachalpradesh.gov.in",), ("arunachal",)),
    Place("assam", ("assam.gov.in",)),
    Place("bihar", ("bihar.gov.in",)),
    Place("chhattisgarh", ("cgstate.gov.in",), ("chhatisgarh",)),
    Place("goa", ("goa.gov.in",)),
    Place("gujarat", ("gujaratindia.gov.in", "gujarat.gov.in")),
    Place("haryana", ("haryana.gov.in",)),
    Place("himachal pradesh", ("hp.gov.in",), ("himachal",)),
    Place("jharkhand", ("jharkhand.gov.in",)),
    Place("karnataka", ("karnataka.gov.in",), ("bengaluru", "bangalore")),
    Place("kerala", ("kerala.gov.in",), ("thiruvananthapuram", "kochi")),
    Place("madhya pradesh", ("mp.gov.in",), ("madhyapradesh",)),
    Place("maharashtra", ("maharashtra.gov.in",), ("mumbai", "pune")),
    Place("manipur", ("manipur.gov.in",)),
    Place("meghalaya", ("meghalaya.gov.in",)),
    Place("mizoram", ("mizoram.gov.in",)),
    Place("nagaland", ("nagaland.gov.in",)),
    Place("odisha", ("odisha.gov.in",), ("orissa",)),
    Place("punjab", ("punjab.gov.in",)),
    Place("rajasthan", ("rajasthan.gov.in",), ("jaipur",)),
    Place("sikkim", ("sikkim.gov.in",)),
    Place("tamil nadu", ("tn.gov.in",), ("tamilnadu", "chennai")),
    Place("telangana", ("telangana.gov.in",), ("hyderabad",)),
    Place("tripura", ("tripura.gov.in",)),
    Place("uttar pradesh", ("up.gov.in",), ("uttarpradesh", "lucknow")),
    Place("uttarakhand", ("uttarakhand.gov.in",), ("uttaranchal",)),
    Place("west bengal", ("wb.gov.in",), ("westbengal", "kolkata")),
    # union territories
    Place("andaman and nicobar", ("andaman.gov.in",), ("andaman", "nicobar")),
    Place("chandigarh", ("chandigarh.gov.in",)),
    Place("dadra and nagar haveli", ("dnh.gov.in",), ("dadra",)),
    Place("daman and diu", ("daman.nic.in",), ("daman", "diu")),
    Place("delhi", ("delhi.gov.in",), ("newdelhi", "ncr")),
    Place("jammu and kashmir", ("jkgad.nic.in", "jammuandkashmir.gov.in"), ("jammu", "kashmir")),
    Place("ladakh", ("ladakh.gov.in",)),
    Place("lakshadweep", ("lakshadweep.gov.in",)),
    Place("puducherry", ("py.gov.in",), ("pondicherry",)),
)


def detect(query: str) -> Place | None:
    """The place a query names, if it names exactly one."""
    lowered = f" {query.lower()} "
    for place in PLACES:
        for word in (place.name, *place.aliases):
            if f" {word} " in lowered:
                return place
    return None


def other_domains(place: Place) -> tuple[str, ...]:
    """Every state portal except this one.

    A page on another state's portal is a wrong answer to a question that named
    this one, however often it repeats the word.
    """
    return tuple(
        domain
        for other in PLACES
        if other.name != place.name
        for domain in other.domains
    )
