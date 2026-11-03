"""The ten rules the external courier must obey, each pinned by a test."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.federated import knowledge_courier as courier

Agent = courier.FreeKnowledgeCourierAgent


# ---------------------------------------------------------------------------
# 1 & 2 — bounded, and never fatal
# ---------------------------------------------------------------------------


def test_the_budget_is_800_ms():
    assert courier.TIMEOUT_SECONDS == 0.8


@pytest.mark.asyncio
async def test_a_hanging_provider_times_out_and_returns_nothing():
    class Slow:
        async def get_json(self, url, timeout=1.5):
            await asyncio.sleep(5)
            return {"extract": "never"}

    card = await Agent(http=Slow(), timeout=0.05).lookup("anything")

    assert card.extract == ""
    assert not card


@pytest.mark.asyncio
async def test_every_failure_yields_an_empty_card_rather_than_an_exception():
    class Broken:
        async def get_json(self, url, timeout=1.5):
            raise RuntimeError("DNS fell over")

    card = await Agent(http=Broken()).lookup("anything")

    assert card.extract == ""


@pytest.mark.asyncio
async def test_a_malformed_payload_is_not_a_crash():
    class Weird:
        async def get_json(self, url, timeout=1.5):
            return {"extract": None, "title": None, "content_urls": "not a dict"}

    card = await Agent(http=Weird()).lookup("anything")

    assert card.extract == ""


# ---------------------------------------------------------------------------
# 3 — the air gap
# ---------------------------------------------------------------------------


def test_the_courier_cannot_reach_the_database():
    """Imports are the air gap: no store, no queue, no client."""
    source = open(courier.__file__, encoding="utf-8").read()

    for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 4 — SSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.0.0.1:8000/x",
        "http://localhost/x",
        "http://10.1.2.3/x",
        "http://172.16.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/x",
    ],
)
def test_a_private_address_is_refused(url):
    allowed, reason = courier.is_public_url(url, resolve=False)

    assert allowed is False
    assert reason in {"private_address", "blocked_hostname", "unresolvable"}


@pytest.mark.parametrize("host", ["postgres", "backend", "redis", "db", "host.docker.internal"])
def test_a_docker_service_name_is_refused(host):
    allowed, reason = courier.is_public_url(f"http://{host}:5432/x", resolve=False)

    assert allowed is False
    assert reason == "blocked_hostname"


# ---------------------------------------------------------------------------
# 6 — protocol whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/html,x"])
def test_only_http_and_https(url):
    allowed, reason = courier.is_public_url(url, resolve=False)

    assert allowed is False
    assert reason == "scheme_not_allowed"


# ---------------------------------------------------------------------------
# 5 — payload size
# ---------------------------------------------------------------------------


def test_the_body_cap_is_half_a_megabyte():
    assert courier.MAX_RESPONSE_BYTES == 512 * 1024


# ---------------------------------------------------------------------------
# 7 — pooling
# ---------------------------------------------------------------------------


def test_the_pool_is_small_and_keepalive_is_bounded():
    assert courier.MAX_KEEPALIVE_CONNECTIONS == 5
    assert courier.MAX_CONNECTIONS == 10


# ---------------------------------------------------------------------------
# 8 — query hygiene
# ---------------------------------------------------------------------------


def test_a_null_byte_is_stripped():
    assert "\x00" not in courier.clean_query("a\x00b")


def test_a_query_is_capped_at_150_characters():
    assert len(courier.clean_query("x" * 400)) == 150


def test_trailing_space_and_newlines_collapse():
    assert courier.clean_query("  Bengaluru \n\n  ").strip() == "Bengaluru"


def test_an_empty_query_returns_nothing_without_a_request():
    class Counting:
        calls = 0

        async def get_json(self, url, timeout=1.5):
            Counting.calls += 1
            return {}

    asyncio.run(Agent(http=Counting()).lookup("   "))

    assert Counting.calls == 0


# ---------------------------------------------------------------------------
# 9 — identity
# ---------------------------------------------------------------------------


def test_the_bot_says_who_it_is():
    assert courier.USER_AGENT.startswith("EyesearchBot/")
    assert "https://eyesearch.in" in courier.USER_AGENT


# ---------------------------------------------------------------------------
# 10 — raw output for downstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_cards_from_the_action_api():
    """The search API, not the REST summary: it ranks, so it answers a query
    rather than an exact title."""

    class Http:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url:
                return {
                    "query": {
                        "search": [
                            {
                                "title": "Bengaluru",
                                "snippet": '<span class="searchmatch">Bengaluru</span> is the capital.',
                            }
                        ]
                    }
                }
            return {}

    cards = await Agent(http=Http()).search("Bengaluru")

    assert len(cards) == 1
    assert cards[0].url == "https://en.wikipedia.org/wiki/Bengaluru"
    assert cards[0].extract == "Bengaluru is the capital."
    assert "<span" not in cards[0].extract


@pytest.mark.asyncio
async def test_candidates_returns_the_plain_shape():
    class Http:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url:
                return {"query": {"search": [{"title": "T", "snippet": "text"}]}}
            return {}

    candidates = await Agent(http=Http()).candidates("q")

    assert candidates == [{"title": "T", "url": "https://en.wikipedia.org/wiki/T", "snippet": "text"}]


@pytest.mark.asyncio
async def test_search_returns_nothing_when_there_is_nothing():
    class Empty:
        async def get_json(self, url, timeout=1.5):
            return {}

    assert await Agent(http=Empty()).search("nonsense") == []
