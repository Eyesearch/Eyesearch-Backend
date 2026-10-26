"""Browser integration: OpenSearch description + autocomplete.

`GET /opensearch.xml` lets Chromium, Firefox and Brave install eyesearch as a
search engine. `GET /v1/suggest` answers the suggestion URL declared in that
document, in the `[query, [completions…]]` shape those browsers expect.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app.config import get_settings
from app.services.search_service import get_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["opensearch"])

_OPENSEARCH_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/"
                       xmlns:moz="http://www.mozilla.org/2006/browser/search/">
  <ShortName>eyesearch</ShortName>
  <Description>eyesearch — a unified search gateway over its own index.</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <OutputEncoding>UTF-8</OutputEncoding>
  <Image width="16" height="16" type="image/svg+xml">{base}/assets/icons/favicon.svg</Image>
  <Url type="text/html" method="GET" template="{base}/?q={{searchTerms}}" />
  <Url type="application/x-suggestions+json" method="GET"
       template="{base}/v1/suggest?q={{searchTerms}}" />
  <Url type="application/opensearchdescription+xml" rel="self"
       template="{base}/opensearch.xml" />
  <moz:SearchForm>{base}/</moz:SearchForm>
  <Developer>EyeSearch</Developer>
  <Contact>https://github.com/Eyesearch/Eyesearch-Backend</Contact>
  <Attribution>Results come from the eyesearch index.</Attribution>
  <SyndicationRight>open</SyndicationRight>
</OpenSearchDescription>
"""


def _public_base(request: Request) -> str:
    """The origin browsers should call back, honouring the proxy's headers."""
    settings = get_settings()
    configured = getattr(settings, "public_base_url", "") or ""
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@router.get(
    "/opensearch.xml",
    summary="OpenSearch description document",
    response_class=Response,
    responses={200: {"content": {"application/opensearchdescription+xml": {}}}},
)
async def opensearch(request: Request) -> Response:
    """Install eyesearch as a browser search engine.

    The suggestion URL is declared here, so browsers get autocomplete for free.
    """
    base = _public_base(request)
    body = _OPENSEARCH_TEMPLATE.format(base=escape(base, {'"': "&quot;"}))
    return Response(
        content=body,
        media_type="application/opensearchdescription+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get(
    "/v1/suggest",
    summary="OpenSearch autocomplete",
    response_description="[query, [completions…]]",
)
async def suggest(
    q: str = Query(default="", max_length=200, description="What the user has typed so far"),
    limit: int | None = Query(default=None, ge=1, le=20),
) -> JSONResponse:
    """Completions drawn from the titles already in our index.

    Returns the OpenSearch suggestions array (`[query, [completions]]`), never an
    error: a bad or empty query yields an empty list so the browser's address bar
    keeps working.
    """
    try:
        completions = await get_service().suggest(q, limit)
    except Exception as exc:  # a suggestion must never break the address bar
        logger.warning("suggest failed for %r: %s", q, exc)
        completions = []
    return JSONResponse([q, completions], headers={"Cache-Control": "public, max-age=60"})
