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
| GET | `/v1/search` | `q` (required, ≤512 chars), `limit` (1–50, default 10), `page` (1–20, default 1) | `SearchResponse` JSON |
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
