"""The place boost: a question that names a place is answered by that place."""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.places import PLACES, detect, other_domains
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


def test_every_state_and_union_territory_is_listed():
    """Twenty-eight states and eight union territories, all of them."""
    names = {place.name for place in PLACES}
    assert len(PLACES) == 37
    for expected in ("karnataka", "tamil nadu", "kerala", "delhi", "ladakh", "puducherry"):
        assert expected in names


def test_a_question_names_its_place():
    assert detect("karnataka scheme status").name == "karnataka"
    assert detect("tamil nadu scheme").name == "tamil nadu"
    assert detect("pm kisan status") is None


def test_another_states_portal_is_not_this_states_answer():
    karnataka = detect("karnataka scheme")
    others = other_domains(karnataka)
    assert "tn.gov.in" in others
    assert "karnataka.gov.in" not in others


@pytest.mark.asyncio
async def test_the_named_states_own_portal_outranks_a_passing_mention(client):
    """The failure this fixes: a Nilgiris page answered a Karnataka question."""
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            _doc(
                "https://karnataka.gov.in/schemes",
                "Karnataka Scheme Status",
                "Karnataka scheme status for every district in the state. " * 3,
                "place-1",
            ),
            _doc(
                "https://nilgiris.nic.in/",
                "Scheme",
                "Karnataka scheme status is mentioned once among many links here. " * 3,
                "place-2",
            ),
        ]
    )

    hits = await store.search("karnataka scheme status", limit=5)

    assert hits[0].url.startswith("https://karnataka.gov.in")


@pytest.mark.asyncio
async def test_another_states_portal_is_not_returned_at_all(client):
    """When the only match is another state's portal, the answer is nothing.

    A Tamil Nadu district page under a Karnataka question is worse than an empty
    result: a reader cannot tell it is the wrong state, and a model will report
    it as the answer.
    """
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            _doc(
                "https://nilgiris.nic.in/",
                "Scheme",
                "Karnataka scheme status is mentioned once among many links here. " * 3,
                "excl-1",
            ),
        ]
    )

    hits = await store.search("karnataka scheme status", limit=5)

    assert hits == []


@pytest.mark.asyncio
async def test_a_non_government_page_about_the_state_is_still_kept(client):
    """Only other states' portals are dropped; a news page about Karnataka stays."""
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            _doc(
                "https://nilgiris.nic.in/",
                "Scheme",
                "Karnataka scheme status is mentioned once among many links here. " * 3,
                "keep-1",
            ),
            _doc(
                "https://karnatakanews.in/schemes",
                "Karnataka scheme status",
                "Karnataka scheme status for every district, explained in full. " * 3,
                "keep-2",
            ),
        ]
    )

    hits = await store.search("karnataka scheme status", limit=5)

    assert hits
    assert all("nilgiris" not in hit.url for hit in hits)


def test_a_district_site_belongs_to_its_state():
    """`nilgiris.nic.in` is Tamil Nadu as surely as `tn.gov.in` is."""
    from app.engine.places import is_other_states_portal

    karnataka = detect("karnataka scheme")
    assert is_other_states_portal("nilgiris.nic.in", karnataka) is True
    assert is_other_states_portal("tn.gov.in", karnataka) is True
    assert is_other_states_portal("karnataka.gov.in", karnataka) is False
    assert is_other_states_portal("bengaluruurban.nic.in", karnataka) is False


def test_a_central_portal_is_never_the_wrong_state():
    """Aadhaar is the same answer in every state."""
    from app.engine.places import is_other_states_portal

    karnataka = detect("karnataka scheme")
    assert is_other_states_portal("uidai.gov.in", karnataka) is False
    assert is_other_states_portal("india.gov.in", karnataka) is False


def test_a_private_page_is_left_alone():
    from app.engine.places import is_other_states_portal

    karnataka = detect("karnataka scheme")
    assert is_other_states_portal("karnatakanews.in", karnataka) is False
