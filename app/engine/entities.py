"""Well-known Indian schemes and services, and the domains that own them.

A reader asks "pm kisan status". The page that answers it says "Pradhan Mantri
Kisan Samman Nidhi" and lives on pmkisan.gov.in. No amount of ranking bridges
that gap: the query and the page share one token at best, and a strict match on
all three words finds nothing at all.

This table is the bridge, and it is deliberately small and hand-checked. Each
entry says: if the question names this thing, the page on this domain is the
answer. It is the same shape as `places.py` — a fact about India, not a
heuristic about text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    name: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...] = ()


ENTITIES: tuple[Entity, ...] = (
    Entity("pm kisan", ("pmkisan.gov.in",), ("pmkisan", "kisan samman", "pm kisan samman")),
    Entity("aadhaar", ("uidai.gov.in",), ("uidai", "aadhar", "aadhaar card")),
    Entity("pan card", ("incometax.gov.in", "onlineservices-nsincometax.gov.in"), ("pan", "nsdl pan")),
    Entity("income tax", ("incometax.gov.in",), ("itr", "income tax return", "e-filing")),
    Entity("gst", ("gst.gov.in",), ("goods and services tax", "gstn")),
    Entity("epfo", ("epfindia.gov.in",), ("pf", "provident fund", "uan", "epf")),
    Entity("passport", ("passportindia.gov.in",), ("passport seva",)),
    Entity("driving licence", ("parivahan.gov.in",), ("dl", "parivahan", "rc status", "vehicle registration")),
    Entity("voter id", ("nvsp.in", "eci.gov.in"), ("voter", "electoral roll", "epic")),
    Entity("mca", ("mca.gov.in",), ("company registration", "din", "ministry of corporate affairs")),
    Entity("rbi", ("rbi.org.in",), ("reserve bank", "repo rate", "rbi rate")),
    Entity("sebi", ("sebi.gov.in",), ("market regulator",)),
    Entity("nrega", ("nrega.nic.in",), ("mgnrega", "job card", "nrega job card")),
    Entity("ayushman bharat", ("ayushmanbharat.gov.in", "pmjay.gov.in"), ("pmjay", "health card")),
    Entity("cowin", ("cowin.gov.in",), ("vaccination certificate", "vaccine")),
    Entity("digilocker", ("digilocker.gov.in",), ("digi locker",)),
    Entity("national scholarship", ("scholarships.gov.in", "nsp.gov.in"), ("nsp", "scholarship")),
    Entity("my scheme", ("myscheme.gov.in",), ("government schemes", "yojana")),
    Entity("india gov", ("india.gov.in",), ("national portal",)),
    Entity("irctc", ("irctc.co.in",), ("railway ticket", "train ticket", "indian railways")),
    Entity("india post", ("indiapost.gov.in",), ("post office", "speed post")),
    Entity("lic", ("licindia.in",), ("life insurance corporation",)),
    Entity("npci", ("npci.org.in",), ("upi", "bhim")),
    Entity("trai", ("trai.gov.in",), ("telecom regulator",)),
    Entity("cbse", ("cbse.gov.in",), ("board results", "cbse result")),
    Entity("nta", ("nta.ac.in",), ("jee", "neet", "ugc net")),
    Entity("upsc", ("upsc.gov.in",), ("civil services", "ias exam")),
    Entity("ssc", ("ssc.nic.in",), ("staff selection",)),
    Entity("ibps", ("ibps.in",), ("bank exam", "po exam")),
    Entity("rrb", ("indianrailways.gov.in",), ("railway recruitment",)),
    Entity("supreme court", ("sci.gov.in",), ("judgment", "case status")),
    Entity("ecourts", ("ecourts.gov.in",), ("case status", "court case")),
    Entity("gazette", ("egazette.gov.in", "egazette.nic.in"), ("gazette notification",)),
    # --- the rest of what people actually ask for -------------------------
    # Added because each of these is a question someone types and a page that
    # says something else: "eshram card" and "e-Shram: One Nation One
    # Ration Card" share no word worth matching on.
    Entity("eshram", ("eshram.gov.in",), ("e shram", "e-shram", "unorganised worker")),
    Entity("umang", ("umang.gov.in",), ("umang app",)),
    Entity("esic", ("esic.gov.in",), ("esic", "employees state insurance")),
    Entity("jansuraksha", ("jansuraksha.gov.in",), ("pmjjby", "pmsby", "suraksha bima")),
    Entity("nps", ("npscra.nsdl.co.in", "pfrda.org.in"), ("national pension", "pension scheme", "atal pension")),
    Entity("irdai", ("irdai.gov.in",), ("insurance regulator",)),
    Entity("gem", ("gem.gov.in",), ("government e marketplace", "gem portal")),
    Entity("enam", ("enam.gov.in",), ("e nam", "national agriculture market", "mandi online")),
    Entity("pmfby", ("pmfby.gov.in",), ("crop insurance", "fasal bima")),
    Entity("jan dhan", ("pmjdy.gov.in",), ("pmjdy", "jan dhan yojana", "bank account scheme")),
    Entity("pm awas", ("pmaymis.gov.in",), ("pmay", "awas yojana", "housing scheme")),
    Entity("ujjwala", ("pmuy.gov.in",), ("pmuy", "lpg connection", "gas connection")),
    Entity("jal jeevan", ("jaljeevanmission.gov.in",), ("jal jeevan", "water connection", "nal se jal")),
    Entity("swachh bharat", ("swachhbharatmission.gov.in",), ("swachh bharat", "toilet scheme")),
    Entity("mudra", ("mudra.org.in",), ("mudra loan", "shishu loan")),
    Entity("standup india", ("standupmitra.in",), ("stand up india", "standupmitra")),
    Entity("abha", ("abha.abdm.gov.in",), ("abha card", "health id", "abdm")),
    Entity("vahan", ("vahan.parivahan.gov.in",), ("vahan", "vehicle registration status")),
    Entity("sarathi", ("sarathi.parivahan.gov.in",), ("sarathi", "licence status", "learning licence")),
    Entity("ntes", ("ntes.indianrailways.gov.in",), ("ntes", "train running status", "train enquiry")),
    Entity("fastag", ("nhai.gov.in",), ("fastag", "toll")),
    Entity("bsnl", ("bsnl.co.in",), ("bsnl", "landline bill")),
    Entity("aiims", ("aiims.edu",), ("aiims", "hospital appointment")),
    Entity("soil health", ("soilhealth.dac.gov.in",), ("soil health card", "soil card")),
    Entity("ration card", ("nfsa.gov.in",), ("ration card", "food security", "pds")),
    # Karnataka's own portals, because that is where this index started and the
    # state's services are named differently from the centre's.
    Entity("karnataka one", ("karnatakaone.gov.in",), ("karnataka one", "seva sindhu", "karnataka services")),
    Entity("bhoomi", ("bhoomi.karnataka.gov.in",), ("bhoomi", "land records", "rtc", "pahani")),
)


def detect(query: str) -> Entity | None:
    """The entity a query names, if it names one.

    The longest alias wins, so "pm kisan samman nidhi" is matched as the scheme
    rather than as whichever shorter alias appears inside it.
    """
    lowered = f" {query.lower()} "
    best: tuple[int, Entity] | None = None
    for entity in ENTITIES:
        for word in (entity.name, *entity.aliases):
            if f" {word} " in lowered and (best is None or len(word) > best[0]):
                best = (len(word), entity)
    return best[1] if best else None
