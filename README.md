# EyeSearch Backend — Unified Search & Retrieval Gateway

Async FastAPI gateway that sits between the EyeSearch web UI and the open web.
It answers from a Turso (LibSQL) cache when it can, falls back to live regional
retrieval (DuckDuckGo, `in-en`) when it cannot, and speaks two different output
contracts: rich cards for the UI and token-optimised context for AI clients.

- Frontend: https://github.com/Eyesearch/Eyesearch
- Backend: this repository
- Deploy target: Render free tier — **one instance, 512 MB RAM**

## Endpoints

| Method | Path | Params | Response |
| --- | --- | --- | --- |
| GET | `/v1/search` | `q`, `limit` (1–50), `page` (1–20), `format` (`cards`\|`json`) | `SearchResponse` cards, or the flat meta-search JSON |
| GET | `/opensearch.xml` | — | OpenSearch description document |
| GET | `/v1/suggest` | `q`, `limit` (1–20) | `[query, [completions…]]` (OpenSearch suggestions) |
| POST | `/v1/retrieve` | body `{"query": str, "format": "markdown"\|"json", "limit": 1–50}` | `text/markdown` (default) or `RetrieveResponse` JSON |
| GET | `/v1/retrieve` | `q`, `format`, `limit` | same as POST, query-string form |
| GET | `/health` · `/healthz` | — | `HealthResponse` JSON, never raises |
| GET | `/` | — | service metadata |
| GET | `/docs` | — | OpenAPI UI |

### `GET /v1/search`

Cache-first: rows for the exact query newer than `CACHE_TTL_HOURS` are served
with `cache_hit: true`. On a miss the live chain runs, results are returned
immediately and persisted to Turso **after** the response is shaped (a background
task; a failed write never reaches the client).

```json
{
  "query": "blade runner",
  "page": 1,
  "limit": 10,
  "total_estimate": 24,
  "took_ms": 412,
  "degraded": false,
  "cache_hit": false,
  "providers": {"duckduckgo": 8, "duckduckgo_html": 2},
  "results": [
    {
      "id": "duckduckgo:8f2a1c9d04",
      "title": "Blade Runner (1982) — IMDb",
      "url": "https://www.imdb.com/title/tt0083658/",
      "snippet": "A blade runner must pursue and terminate four replicants…",
      "source": "duckduckgo",
      "kind": "film",
      "poster_url": null,
      "thumbnail_url": null,
      "published_at": null,
      "score": 1.0
    }
  ]
}
```

`total_estimate` is an estimate (cached count when available, otherwise the
result count) and is documented as such — it is not an exact index count.
`poster_url`/`thumbnail_url` are `null` unless the provider supplied an image or
the URL is a YouTube video, whose thumbnail is deterministic from the video id.
Nothing is fabricated.

### `POST /v1/retrieve`

```bash
curl -s -X POST localhost:8000/v1/retrieve \
  -H 'content-type: application/json' \
  -d '{"query": "espresso machine buying guide", "format": "markdown", "limit": 5}'
```

```markdown
# espresso machine buying guide

1. **Espresso - Wikipedia**
   https://en.wikipedia.org/wiki/Espresso
   Espresso is a concentrated form of coffee brewed by forcing hot water…

2. **Home espresso buying guide**
   https://www.example.com/guide
   What to look for in a first machine.
```

Markdown mode returns `text/markdown` with no JSON envelope: title, URL, and the
minimum prose per result, snippets trimmed at sentence boundaries, tracking
parameters stripped, boilerplate removed. When part of the chain failed, one
trailing line is appended: `> Partial result: 1 provider unavailable.`

`format: "json"` returns `{query, format, degraded, providers, failed_providers,
results[{title, url, snippet}]}` — the UI shape minus presentation fields.

### Errors

Both endpoints share one envelope:

```json
{"error": {"code": "provider_unavailable", "message": "no providers returned results", "retryable": true}}
```

Codes: `bad_request` (400), `not_found` (404), `rate_limited` (429),
`provider_unavailable` (503), `database_error` (503), `internal` (500).
`retryable` is a real judgement — it is `false` for `bad_request`.

## Consumer protocols

One index, four ways in — each shaped for a different consumer:

**Browsers.** `GET /opensearch.xml` returns an
`application/opensearchdescription+xml` document (`ShortName: eyesearch`) with
both a `text/html` search URL and a `application/x-suggestions+json` URL, so
Chromium, Firefox and Brave can install eyesearch as a search engine and get
autocomplete in the address bar. The template origin comes from
`PUBLIC_BASE_URL` when set, otherwise from the request's forwarded headers.

**Autocomplete.** `GET /v1/suggest?q=bengaluru+metro` →
`["bengaluru metro", ["Bengaluru Metro Purple Line timings"]]`, drawn from the
titles in `documents_fts`, deduplicated, with a prefix fallback so a half-typed
word still completes. It never errors — a bad query returns `[q, []]`.

**Meta-search aggregators.** `GET /v1/search?q=…&format=json`:

```json
{
  "query": "bengaluru metro",
  "total_hits": 1,
  "took_ms": 18,
  "source": "eyesearch_index",
  "results": [
    {"title": "Bengaluru Metro Purple Line timings",
     "url": "https://english.bmrc.co.in/metro-timings",
     "snippet": "Purple Line services run 05:00 to 23:00 on weekdays.",
     "favicon": "https://english.bmrc.co.in/favicon.ico"}
  ]
}
```

`source` is `eyesearch_index`, `eyesearch_cache` or `eyesearch_queued`. Without
`format=json` the endpoint keeps returning the card shape the web UI uses.

**AI / LLM agents (RAG).** `GET /v1/retrieve?q=…&max_results=5`:

```json
{
  "query": "bengaluru metro",
  "source": "index",
  "context": [
    {"title": "Bengaluru Metro Purple Line timings",
     "url": "https://english.bmrc.co.in/metro-timings",
     "text": "Purple Line services run 05:00 to 23:00 on weekdays."}
  ]
}
```

Clean text only — no HTML, no navigation, no boilerplate, snippets trimmed at a
word boundary. `format=markdown` returns the same content as a markdown document
for prompt assembly, and `POST /v1/retrieve` keeps its original contract (now
carrying `context` alongside the legacy `results`).

## Autonomous backlog worker

A missed query is written to `crawl_backlog` by the request path and resolved by
`app/indexer/backlog_worker.py`, off the request path:

1. pop pending queries (`BACKLOG_BATCH_QUERIES`, default 3 per pass)
2. work out candidate URLs — seed portals whose label matches the query, plus
   optional provider discovery (`BACKLOG_DISCOVERY_ENABLED`), which happens here
   and never while a user waits
3. filter through the same `is_indexable()` rules, then order by authority
   (government → news → other, shallower paths first), capped at
   `BACKLOG_URLS_PER_QUERY` (5)
4. fetch with the crawler's politeness and byte caps, extract to the same 2 KB
   budget, ingest under the same dedupe rules
5. mark the query processed **only if something was actually indexed** — an
   unresolved query stays pending for the next pass

The loop runs from the gateway lifespan (`BACKLOG_WORKER_ENABLED`,
`BACKLOG_INTERVAL_SECONDS` default 900, jittered). Verified end to end: a query
returning `source: eyesearch_queued, total_hits: 0` became
`source: eyesearch_index, total_hits: 1` after one worker pass.

## Own index: crawler + indexer

The gateway is no longer only a proxy. `app/engine/`, `app/crawler/` and
`app/indexer/` add a proprietary index that answers queries before any external
provider is touched.

### Storage budget: 2 KB per page, enforced

The index stores answers, not copies. A row is capped at
`INDEX_MAX_PAYLOAD_BYTES` (2048 by default) and holds only:

| field | limit |
| --- | --- |
| `title` | ≤ 100 characters, cut at a word boundary |
| `canonical_url` | normalised (no tracking, no trailing slash, no hash) |
| `snippet` | meta description, else the first ~200 clean characters |
| `favicon` | absolute URL |
| `language` | `en` / `kn` / `hi`, detected by script |
| `content` | a short lead only, so FTS5 has text to match — ≤ 1200 chars |

Markup, scripts, stylesheets, inline images, `data:` URIs, base64 blobs,
tracking pixels and paragraph dumps are stripped before storage, and
`ExtractedDocument.fit_to_budget()` trims content → snippet → title until the
whole row fits. A real page comes out around 0.3–1 KB.

### Deduplication and crawl scope

- URLs are normalised through one function (`app/engine/urls.normalise_url`):
  lowercase host, `www.` removed, `utm_*`/`ref`/`gclid`/`fbclid`/`source`/
  `campaign` and friends dropped, query keys sorted, trailing slash and hash
  removed. That normalised string is the primary key.
- Ingestion **rejects** a URL that already exists — it does not overwrite. Only
  revalidation updates a row, and it updates in place.
- At most `INDEX_MAX_URLS_PER_DOMAIN` (12) URLs per registrable domain, counting
  `.gov.in`/`.nic.in`/`.co.in` style suffixes as one unit.
- `is_indexable()` keeps the index to entry portals: path depth ≤ 2, no
  `/commit/`, `/pulls/`, `/issues`, `/blob/`, `/tree/`, `/search`, `/login`,
  cart/checkout paths, pagination (`/page/2`, `?page=3`), long dynamic query
  chains, repository `owner/repo` loops, and non-document extensions.

**Schema** (applied at startup with the rest of the DDL):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    url, title, snippet, content, language, updated_at UNINDEXED
);
```

backed by `documents` (one row per URL: canonical URL, domain, title, snippet,
clean body, language, favicon, timestamps), `crawl_backlog` (queries the index
could not answer) and `crawl_queue` (URLs to fetch).

**Crawler** (`app/crawler/`) — targeted, not general: a seed list of Indian and
regional domains (`.gov.in`, `.nic.in`, Karnataka government, BMRC, ISRO,
Kannada Wikipedia, Prajuna/Deccan Herald/The Hindu Karnataka), robots.txt
honoured, one request at a time per host with a delay, a byte cap per page, a
page budget per seed, same-host links only, and an SSRF guard that refuses
link-local hosts. Every failure is skipped, never fatal.

**Extractor** (`app/engine/text.py`) — title (falling back to `<h1>`), meta
description, canonical URL, favicon, and a clean body with navigation, footers,
scripts and forms stripped. It detects Kannada and Devanagari by script, and
refuses pages with no title or almost no text.

**Ranking** (`app/engine/ranking.py`) — SQLite's `bm25()` flipped to
higher-is-better, normalised across the result set, then blended with an
exponential freshness decay (30-day half-life) and a small host-trust term that
favours official and regional domains.

**Routing** (`app/services/search_service.py`) — cache-first, and never live:

```
documents_fts  →  (≥ INDEX_THRESHOLD hits)  →  source: "index"
              →  Turso result cache         →  source: "cache"
              →  miss                       →  source: "queued", query added to crawl_backlog
```

The request path performs **no external HTTP**. A miss returns immediately
(measured 3–5 ms warm in the smoke test) with an empty result set and
`source: "queued"`; the background crawler picks the query up. Set
`LIVE_SEARCH_ENABLED=true` only as an emergency escape hatch — the smoke suite
covers that path, the default is off.

Every response now carries `source` (`index` | `cache` | `live`) and
`index_hits`, so a client can tell whether the answer came from our own index.
`/health` reports `index: {documents, backlog}`.

**Stale-while-revalidate** (`app/indexer/revalidate.py`, loop in
`app/indexer/scheduler.py`, started from the gateway lifespan):

| domain class | recheck |
| --- | --- |
| news portals | every 1.5 days (`REVALIDATE_NEWS_DAYS`) |
| `.gov.in`, `.nic.in`, ksrtc… | every 7 days (`REVALIDATE_GOV_DAYS`) |
| static / reference | every 21 days (`REVALIDATE_STATIC_DAYS`) |

Each recheck is a conditional GET with `If-None-Match` / `If-Modified-Since`.
A 304 means **zero database writes** — no row update, no FTS rewrite; a changed
page is re-extracted to the same 2 KB budget and UPDATEs the existing row in
place, so a URL can never appear twice. Unchanged pages intentionally write
nothing, so the worker keeps an in-memory cooldown to avoid re-fetching the same
URL on the next pass.

**Running the ingestion** (a scheduler can call the same functions):

```bash
python -m app.indexer.pipeline --stats                  # index health
python -m app.indexer.pipeline --seeds                  # crawl + index the seed domains
python -m app.indexer.pipeline --query "namma metro"    # crawl the seeds relevant to one query
python -m app.indexer.pipeline --backlog                # crawl for the queries we could not answer
python -m app.indexer.pipeline --revalidate             # one stale-while-revalidate pass
```

Engine knobs (all optional): `INDEX_THRESHOLD` (default 1),
`INDEX_FRESHNESS_WEIGHT` (0.25), `INDEX_MAX_PAYLOAD_BYTES` (2048),
`INDEX_TITLE_MAX_CHARS` (100), `INDEX_SNIPPET_MAX_CHARS` (200),
`INDEX_CONTENT_MAX_CHARS` (1200), `INDEX_MAX_URLS_PER_DOMAIN` (12),
`INDEX_MAX_PATH_DEPTH` (2), `LIVE_SEARCH_ENABLED` (false), `PUBLIC_BASE_URL` ("")
`SUGGEST_LIMIT` (8), `BACKLOG_WORKER_ENABLED` (true),
`BACKLOG_INTERVAL_SECONDS` (900), `BACKLOG_BATCH_QUERIES` (3),
`BACKLOG_URLS_PER_QUERY` (5), `BACKLOG_DISCOVERY_ENABLED` (true),
`REVALIDATE_ENABLED` (true), `REVALIDATE_INTERVAL_SECONDS` (3600),
`REVALIDATE_NEWS_DAYS` (1.5), `REVALIDATE_GOV_DAYS` (7),
`REVALIDATE_STATIC_DAYS` (21), `CRAWL_ENABLED` (true),
`CRAWL_MAX_PAGES_PER_SEED` (20), `CRAWL_MAX_DEPTH` (1), `CRAWL_CONCURRENCY` (3),
`CRAWL_DELAY_SECONDS` (1.0), `CRAWL_MAX_BYTES` (512000),
`CRAWL_RESPECT_ROBOTS` (true), `INDEX_MAX_CONTENT_CHARS` (20000).

## Retrieval fallback chain

```
Turso cache → DuckDuckGo library → DuckDuckGo HTML → DuckDuckGo Lite → (opt-in: Bing HTML) → degraded empty result
```

- Per-hop budgets: library 3.5 s, HTML 3.0 s, Lite 2.5 s, Bing 3.0 s; whole chain
  7.0 s. All configurable (`DDGS_BUDGET_S`, `HTML_BUDGET_S`, `LITE_BUDGET_S`,
  `BING_BUDGET_S`, `CHAIN_BUDGET_S`).
- The library hop carries a worker-thread cold start of roughly 2.5 s on a fresh
  instance, which is why its budget is the largest.
- DuckDuckGo answers non-browser clients with HTTP 202 and a challenge page from
  some IP ranges. That is detected and reported as `blocked`, so the chain moves
  on instead of parsing a challenge page.
- The fourth hop (Bing HTML, with `/ck/a` redirect decoding) is **off by default**
  via `ENABLE_BING_SCRAPE=false`: Bing's terms discourage automated scraping, so
  turning it on is the owner's call. The three DuckDuckGo hops are always on.
- A provider that times out, errors, rate-limits or returns unparseable markup
  is recorded and skipped — the response is still `200` with `degraded: true`
  and the failing provider names. A dead provider never becomes a 500.
- Circuit breaker: opens after 5 consecutive failures, half-opens after 60 s.
  In-memory dict, no Redis. Breaker state is exposed on `/health`.
- Fan-out is capped by a global semaphore (`MAX_CONCURRENT_LIVE`, default 4).
- The DuckDuckGo library is synchronous, so it runs in a worker thread
  (`asyncio.to_thread`) and is awaited under a timeout. The scrape hops are pure
  async `httpx`.

## Turso schema

Applied idempotently at startup (`CREATE TABLE IF NOT EXISTS`), so Render needs
no migration runner:

```sql
CREATE TABLE IF NOT EXISTS search_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT,
    title TEXT,
    url TEXT UNIQUE,
    snippet TEXT,
    poster_url TEXT,
    source TEXT DEFAULT 'live_crawl',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_query ON search_records(query);
CREATE INDEX IF NOT EXISTS idx_query_created ON search_records(query, created_at);
CREATE INDEX IF NOT EXISTS idx_created_at ON search_records(created_at);
```

Cache reads are `WHERE query = ? AND created_at >= now - CACHE_TTL_HOURS`.
Writes are one batched round trip with `ON CONFLICT(url) DO UPDATE`. Because
`url` is unique, a URL seen under a newer query moves to that query rather than
duplicating; a row therefore belongs to the most recent query that produced it.
Rows older than `7 × CACHE_TTL_HOURS` are purged at startup to keep the free-tier
database small.

## Configuration

Copy `.env.example` to `.env`. Every variable has a working default; with an
empty environment the service boots against `file:./local.db`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TURSO_DB_URL` (or `TURSO_DATABASE_URL`) | `file:./local.db` | LibSQL URL |
| `TURSO_AUTH_TOKEN` | `""` | Turso token; empty means local file mode |
| `PORT` | `8000` | Listen port (Render injects this) |
| `ENVIRONMENT` | `development` | `development` / `production` / `test` |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `CORS_ORIGINS` | `*` | Comma-separated origins or `*` |
| `SEARCH_REGION` | `in-en` | DuckDuckGo region (India, English) |
| `SAFESEARCH` | `moderate` | `on` / `moderate` / `off` |
| `ENABLE_BING_SCRAPE` | `false` | Enable the opt-in Bing HTML hop |
| `CACHE_TTL_HOURS` | `24` | Cache freshness window |
| `CACHE_MIN_RESULTS` | `1` | Rows needed to count as a cache hit |
| `DEFAULT_LIMIT` / `MAX_LIMIT` | `10` / `50` | Server-side result caps |
| `SNIPPET_MAX_CHARS` / `AI_SNIPPET_MAX_CHARS` | `260` / `200` | Snippet trimming |
| `MAX_CONCURRENT_LIVE` | `4` | Live-retrieval concurrency cap |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/health
curl -s 'localhost:8000/v1/search?q=isro%20chandrayaan&limit=5' | python -m json.tool
```

Tests (offline — live retrieval is stubbed):

```bash
pytest -q
```

## Deploy on Render

1. Create a Web Service from this repo (or point Render at `render.yaml`).
2. Build: `pip install -r requirements.txt`.
   Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
   Health check path: `/health`.
3. Set `TURSO_DB_URL` and `TURSO_AUTH_TOKEN` as environment variables. Never
   commit them.
4. Keep `--workers 1`: one 512 MB instance, and the cache/breaker state is
   in-process by design.

## Layout

```
app/
├── main.py                 # app factory, lifespan, CORS, exception handlers
├── config.py               # pydantic-settings, env fallbacks
├── database.py             # LibSQL async client, schema, typed queries
├── errors.py               # AppError family + error envelope
├── schemas.py              # Pydantic v2 contract
├── engine/                 # own index: text extraction, schema, ranking, store
├── crawler/                # targeted crawler for Indian/regional domains
├── indexer/                # ingestion pipeline + CLI
├── services/
│   ├── live_retrieval.py   # fallback chain, per-hop timeouts, circuit breaker
│   ├── search_service.py   # cache → live → async persist orchestration
│   └── formatter.py        # UI cards, AI markdown/JSON, dedup, snippet cleaning
└── routes/
    ├── search.py           # GET /v1/search
    ├── retrieve.py         # POST|GET /v1/retrieve
    └── health.py           # GET /health, /healthz
tests/                      # offline smoke tests
requirements.txt · .env.example · render.yaml · pytest.ini
```
