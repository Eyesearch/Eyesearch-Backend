"""Step 3: the rate limiter, the storage watchdog, query sanitisation and the
AI-shaped search endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.indexer.rate_limit import TokenBucketLimiter
from app.indexer.watchdog import MODE_FRESHNESS, MODE_NORMAL, StorageWatchdog, evaluate
from app.services.search_service import sanitise_query


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_a_client_may_spend_its_bucket_then_is_refused():
    now = [0.0]
    limiter = TokenBucketLimiter(capacity=60, window_seconds=60.0, clock=lambda: now[0])

    allowed = sum(1 for _ in range(60) if limiter.check("1.2.3.4").allowed)
    assert allowed == 60

    refused = limiter.check("1.2.3.4")
    assert refused.allowed is False
    assert refused.retry_after_seconds > 0


def test_the_bucket_refills_over_time():
    now = [0.0]
    limiter = TokenBucketLimiter(capacity=60, window_seconds=60.0, clock=lambda: now[0])
    for _ in range(60):
        limiter.check("1.2.3.4")
    assert limiter.check("1.2.3.4").allowed is False

    now[0] = 1.0  # one token per second at this capacity
    assert limiter.check("1.2.3.4").allowed is True


def test_one_client_cannot_spend_another_client_s_bucket():
    now = [0.0]
    limiter = TokenBucketLimiter(capacity=2, window_seconds=60.0, clock=lambda: now[0])
    limiter.check("a")
    limiter.check("a")

    assert limiter.check("a").allowed is False
    assert limiter.check("b").allowed is True


# ---------------------------------------------------------------------------
# Storage watchdog
# ---------------------------------------------------------------------------


def test_the_watchdog_switches_mode_at_the_threshold():
    assert evaluate(900, 1000, 0.95).mode == MODE_NORMAL
    assert evaluate(950, 1000, 0.95).mode == MODE_FRESHNESS
    assert evaluate(1000, 1000, 0.95).mode == MODE_FRESHNESS


def test_the_threshold_is_a_fraction_of_the_ceiling():
    state = evaluate(96, 100, 0.95)
    assert state.over_ceiling is True
    assert round(state.fraction, 2) == 0.96


def test_a_zero_ceiling_does_not_divide_by_zero():
    state = evaluate(10, 0)
    assert state.limit_bytes == 1
    assert state.mode == MODE_FRESHNESS


@pytest.mark.asyncio
async def test_the_watchdog_reads_the_real_database_size(client, settings):
    settings.max_storage_mb = 10_000.0
    state = await StorageWatchdog(client, settings).state()

    assert state is not None
    assert state.bytes_used > 0
    assert state.mode == MODE_NORMAL


# ---------------------------------------------------------------------------
# Query sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("health notifications", "health notifications"),
        ("a\x00b", "ab"),
        ("<script>alert(1)</script>news", "news"),
        ("<b>bold</b> query", "bold query"),
        ("line\x01break", "linebreak"),
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_a_query_is_cleaned(raw, expected):
    assert sanitise_query(raw) == expected


def test_a_query_is_capped():
    assert len(sanitise_query("x" * 400)) == 256


# ---------------------------------------------------------------------------
# The AI-shaped endpoint and OpenSearch
# ---------------------------------------------------------------------------


def test_the_ai_endpoint_returns_the_dense_shape(client):
    from app.main import app

    app.state.service = _StubService()
    response = TestClient(app).get("/v1/ai/search", params={"q": "health"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    item = body["results"][0]
    assert set(item) == {"title", "brand", "clean_url", "category", "snippet", "url"}
    # Capped by the same ceiling the index uses, not a separate number.
    from app.engine.config import get_settings

    assert len(item["snippet"]) <= get_settings().page_body_char_ceiling


def test_the_ai_endpoint_can_answer_in_markdown(client):
    from app.main import app

    app.state.service = _StubService()
    response = TestClient(app).get(
        "/v1/ai/search", params={"q": "health", "format": "markdown"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# health")


def test_opensearch_describes_both_endpoints(client):
    from app.main import app

    response = TestClient(app).get("/opensearch.xml")

    assert response.status_code == 200
    assert "application/opensearchdescription+xml" in response.headers["content-type"]
    assert "/v1/search?q=" in response.text
    assert "/v1/suggest?q=" in response.text


class _StubService:
    """Just enough of SearchService for the endpoint's shape."""

    async def search(self, query, limit=None, **kwargs):
        return {
            "results": [
                {
                    "title": "District Health Notifications",
                    "brand_name": "Karnataka Health",
                    "clean_url": "karnataka.gov.in › health",
                    "category": "GOV",
                    "snippet": "x" * 200,
                    "url": "https://karnataka.gov.in/health",
                }
            ]
        }
