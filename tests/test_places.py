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
