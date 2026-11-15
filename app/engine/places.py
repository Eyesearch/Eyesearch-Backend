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
    # Karnataka's district hosts are listed because they are the ones seeded:
    # the state portal answers 403 to this crawler, so its districts are how
    # Karnataka content is reached. A district host that is not listed here
    # cannot be told apart from another state's, which is the cost of not
    # carrying all 700-odd of them.
    Place(
        "karnataka",
        (
            "karnataka.gov.in",
            "bengaluruurban.nic.in", "bengalurural.nic.in", "bagalkot.nic.in",
            "belgaum.nic.in", "bellary.nic.in", "bidar.nic.in", "bijapur.nic.in",
            "chamarajanagar.nic.in", "chickmagalur.nic.in", "chikkaballapur.nic.in",
            "chitradurga.nic.in", "davanagere.nic.in", "dharwad.nic.in",
            "gadag.nic.in", "gulbarga.nic.in", "hassan.nic.in", "haveri.nic.in",
            "kodagu.nic.in", "kolar.nic.in", "koppal.nic.in", "mandya.nic.in",
            "mysore.nic.in", "raichur.nic.in", "shimoga.nic.in", "tumkur.nic.in",
            "udupi.nic.in", "uttarakannada.nic.in", "yadgir.nic.in",
        ),
        ("bengaluru", "bangalore"),
    ),
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


#: Central portals answer questions about every state, so they are never the
#: wrong state's answer. Everything else on a government suffix belongs to one
#: state or one of its districts.
CENTRAL_DOMAINS: frozenset[str] = frozenset(
    {
        "india.gov.in", "uidai.gov.in", "incometax.gov.in", "gst.gov.in",
        "epfindia.gov.in", "passportindia.gov.in", "parivahan.gov.in",
        "nrega.nic.in", "myscheme.gov.in", "nvsp.in", "mca.gov.in",
        "rbi.org.in", "sebi.gov.in", "ayushmanbharat.gov.in", "pmjay.gov.in",
        "cowin.gov.in", "digilocker.gov.in", "scholarships.gov.in",
        "nsp.gov.in", "egazette.gov.in", "egazette.nic.in", "eci.gov.in",
        "sci.gov.in", "ecourts.gov.in", "upsc.gov.in", "cbse.gov.in",
        "nta.ac.in", "ssc.nic.in", "ibps.in", "indianrailways.gov.in",
        "indiapost.gov.in", "licindia.in", "npci.org.in", "trai.gov.in",
    }
)

#: Suffixes that mark a government host.
GOVERNMENT_SUFFIXES: tuple[str, ...] = (".gov.in", ".nic.in", ".ac.in", ".res.in")


def is_other_states_portal(domain: str, place: Place) -> bool:
    """Whether this host speaks for a state other than the one named.

    A district site — `nilgiris.nic.in` — belongs to Tamil Nadu as surely as
    `tn.gov.in` does, and neither belongs in an answer to a Karnataka question.
    The state portals alone were not enough: the page that answered "karnataka
    scheme status" was a district site, which no state table names.
    """
    host = domain.lower().removeprefix("www.")
    if not host:
        return False
    if host in place.domains or host in CENTRAL_DOMAINS:
        return False
    if any(host == portal or host.endswith("." + portal) for portal in place.domains):
        return False
    return any(host.endswith(suffix) for suffix in GOVERNMENT_SUFFIXES)
