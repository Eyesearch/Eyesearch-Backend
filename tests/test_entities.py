"""Schemes and services, and the domains that own them.

`pm kisan status` and the page that answers it — "Pradhan Mantri Kisan Samman
Nidhi" on pmkisan.gov.in — share one token. Ranking cannot bridge that; a table
can.
"""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.entities import ENTITIES, detect
from app.engine.store import IndexStore, PageDocument


def _doc(url: str, title: str, body: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("//", 1)[1].split("/", 1)[0],
        title=title,
        description=f"{title} description.",
        body_text=body,
        digest=digest,
    )


@pytest.mark.parametrize(
    "question,domain",
    [
        ("pm kisan status", "pmkisan.gov.in"),
        ("aadhaar update", "uidai.gov.in"),
        ("gst return filing", "gst.gov.in"),
        ("epfo uan login", "epfindia.gov.in"),
        ("pan card apply", "incometax.gov.in"),
    ],
)
def test_a_scheme_is_recognised_by_the_name_people_use(question, domain):
    entity = detect(question)
    assert entity is not None
    assert domain in entity.domains


def test_the_longest_alias_wins():
    """`pm kisan samman nidhi` is the scheme, not whichever alias sits inside it."""
    entity = detect("pm kisan samman nidhi beneficiary status")
    assert entity is not None and entity.name == "pm kisan"


def test_a_question_about_nothing_named_returns_nothing():
    assert detect("karnataka scheme status") is None


def test_every_entity_names_at_least_one_domain():
    for entity in ENTITIES:
        assert entity.domains, entity.name


@pytest.mark.asyncio
async def test_the_owning_domain_outranks_a_page_that_repeats_the_words(client):
    """The failure: an unrelated page that repeats "status" beat the scheme's site."""
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            _doc(
                "https://pmkisan.gov.in/",
                "Pradhan Mantri Kisan Samman Nidhi",
                "Beneficiary list and instalment details for registered farmers. " * 3,
                "ent-1",
            ),
            _doc(
                "https://tamenglong.nic.in/land",
                "Government Land Status",
                "Land status and pm kisan status are mentioned in this district page. " * 3,
                "ent-2",
            ),
        ]
    )

    hits = await store.search("pm kisan status", limit=5)

    assert hits, "the scheme's own page must be found"
    assert hits[0].url.startswith("https://pmkisan.gov.in")
