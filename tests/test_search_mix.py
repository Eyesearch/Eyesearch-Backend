import pytest
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.services.search_service import SearchService

BODY = "phonepe upi payment app india wallet transfer " * 3


def pg(url, digest, brand):
    return PageDocument(url=url, domain=url.split("/")[2], title=f"phonepe {digest}",
                        description="phonepe", body_text=BODY, digest=digest,
                        brand_name=brand)


@pytest.mark.asyncio
async def test_service_mixes_other_sites(client):
    store = IndexStore(client, Settings(environment="test"))
    pages = [pg(f"https://phonepe.com/p{i}", f"b{i}", "PhonePe") for i in range(8)]
    pages += [pg("https://businessworld.in/phonepe", "o1", "Businessworld"),
              pg("https://cashfry.in/phonepe", "o2", "Cashfry")]
    await store.persist_batch(pages)

    service = SearchService(store, Settings(environment="test"))
    payload = await service.search("phonepe", limit=10)
    brands = [r["brand_name"] for r in payload["results"]]
    print("BRANDS", brands)
    assert len(payload["results"]) == 10
    assert any(b != "PhonePe" for b in brands)
