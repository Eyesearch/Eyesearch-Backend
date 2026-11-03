"""The courier's truthiness, fallback, identity and contract."""

from __future__ import annotations

import pytest

from app.agents.federated import knowledge_courier as courier

Agent = courier.FreeKnowledgeCourierAgent
Card = courier.KnowledgeCard


# ---------------------------------------------------------------------------
# Truthiness — the silent-drop rule
# ---------------------------------------------------------------------------


def test_an_empty_card_is_false():
    assert bool(Card()) is False


def test_a_card_with_an_extract_is_true():
    assert bool(Card(title="T", url="https://x.in", extract="an answer")) is True


def test_a_card_with_an_extract_but_no_url_is_still_true():
    """Wikipedia sometimes omits content_urls.

    A summary with no canonical link is still an answer. Requiring a URL here
    would discard it — which is the silent drop, not the fix for it.
    """
    assert bool(Card(title="T", url="", extract="an answer")) is True


def test_a_card_with_only_a_title_and_url_is_false():
    """No extract means nothing to show. A card is its text, not its address."""
    assert bool(Card(title="T", url="https://x.in")) is False


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_falls_back_to_duckduckgo_when_wikipedia_is_empty():
    seen: list[str] = []

    class Http:
        async def get_json(self, url, timeout=1.5):
            seen.append("wikipedia" if "wikipedia" in url else "duckduckgo")
            if "wikipedia" in url:
                return {}
            return {
                "Heading": "Bengaluru",
                "AbstractText": "Bengaluru is the capital of Karnataka.",
                "AbstractURL": "https://example.in/bengaluru",
            }

    card = await Agent(http=Http()).lookup("Bengaluru")

    assert seen == ["wikipedia", "duckduckgo"]
    assert card.source == "duckduckgo"
    assert card.extract.startswith("Bengaluru is")


@pytest.mark.asyncio
async def test_lookup_stops_at_wikipedia_when_it_answers():
    seen: list[str] = []

    class Http:
        async def get_json(self, url, timeout=1.5):
            seen.append("wikipedia" if "wikipedia" in url else "duckduckgo")
            return {
                "title": "Bengaluru",
                "extract": "Bengaluru is the capital of Karnataka.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Bengaluru"}},
            }

    card = await Agent(http=Http()).lookup("Bengaluru")

    assert seen == ["wikipedia"]
    assert card.source == "wikipedia"


# ---------------------------------------------------------------------------
# Identity — Wikimedia 403s a User-Agent with no contact route
# ---------------------------------------------------------------------------


def test_the_user_agent_names_a_contact_route():
    assert courier.USER_AGENT.startswith("EyesearchBot/")
    assert "https://eyesearch.in" in courier.USER_AGENT
    # Wikimedia's policy asks for a contact; a bare bot name is refused.
    assert "@" in courier.USER_AGENT


def test_the_client_sends_the_agent_and_accepts_json(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    Agent()._ensure_client()

    headers = captured.get("headers", {})
    assert headers.get("User-Agent") == courier.USER_AGENT
    assert headers.get("Accept") == "application/json"
    assert captured.get("follow_redirects") is False


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_the_dns_budget_is_half_a_second_and_inside_the_request_budget():
    assert courier.DNS_TIMEOUT_SECONDS == 0.5
    assert courier.DNS_TIMEOUT_SECONDS < courier.TIMEOUT_SECONDS == 0.8


def test_the_concurrency_cap_and_air_gap_are_unchanged():
    assert courier.MAX_CONCURRENT_REQUESTS == 5
    source = open(courier.__file__, encoding="utf-8").read()
    for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_the_contract_audits_clean():
    assert courier.CONTRACT["agent"] == "federated.knowledge_courier"
    courier.audit_contract()


def test_telemetry_is_exposed_to_the_supervisor():
    assert callable(courier.telemetry)
