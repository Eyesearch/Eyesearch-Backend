# EyeSearch Backend

An async FastAPI gateway over our own Turso (libSQL) index. It crawls
authoritative Indian domains through a six-stage pipeline, stores one bounded
row per page, and answers four different consumer contracts from that single
index: result cards for the web UI, suggestions for the address bar, token
budgets for AI clients, and a grounded chat stream for the AI tab.

- Frontend: https://github.com/Eyesearch/Eyesearch
- Deploy target: Render free tier — **one instance, 512 MB RAM**
- Database: Turso (libSQL) over HTTPS. The free Render instance has no
  persistent disk, so the database is never local in production.

## Repository layout

```text
app/
├── main.py                     FastAPI app, lifespan, CORS, every HTTP route
├── database.py                 Turso connection bootstrap, schema, write probe
├── libsql_http.py              Turso HTTP client + ProcessRateLimiter (2 req/s)
├── orchestrator.py             Shared pooled httpx client, pipeline wiring
├── agents/
│   ├── 01_frontier_agent.py    Queue depth, priority, seed refill
│   ├── 02_fetcher_agent.py     Pooled HTTP, bound headers, retry handling
│   ├── 03_parser_agent.py      HTML strip, metadata, 1,000-char body cap
│   ├── 04_quality_agent.py     80-char density check, anti-junk, SHA-256 dedupe
│   ├── 05_indexer_agent.py     Fail-closed storage guard, atomic persistence
│   └── 06_revalidation_agent.py Background stale-page revalidation
├── engine/
│   ├── config.py               Pydantic BaseSettings — every knob
│   └── store.py                IndexStore: atomic batches, FTS5 search, stats
├── indexer/
│   ├── crawler.py              run_url_pass + a CLI (--probe, --refill, --once)
│   ├── guard.py                60s TTL count cache, fail-closed storage_state
│   └── scheduler.py            Non-blocking worker loops
└── services/
    └── search_service.py       The query service the frontend consumes
tests/
├── test_rate_limiter.py
├── test_parser_cap.py
├── test_quality_gate.py
└── test_persistence_atomic.py
```

The agent files are numbered because the brief names them that way. Python
cannot import a module whose name begins with a digit using `import` syntax, so
they are reached through `app.agents.load_agent("03_parser_agent")`. That is the
one wart in the layout, and it is contained in `app/agents/__init__.py`.

## Endpoints

| Method | Path | Consumer |
| --- | --- | --- |
| GET | `/v1/search?q=&limit=&page=` | result cards for the web UI |
| GET | `/v1/search?q=&format=json` | flat meta-search contract |
| GET | `/v1/suggest?q=&limit=` | address-bar autocomplete (`[query, [items]]`) |
| GET · POST | `/v1/retrieve` | AI clients — `json` (default) or `markdown` |
| POST | `/v1/chat` | AI tab, server-sent events |
| GET | `/v1/stats` | index counters and storage state |
| GET | `/health` · `/healthz` | Render probe, never raises |
| GET | `/opensearch.xml` | browser search-engine install |
| GET | `/docs` | OpenAPI UI |

Errors leave through one envelope, because `web/config/api-config.js` maps
`code` onto user-facing text and treats a missing `retryable` as true:

```json
{"error": {"code": "bad_request", "message": "A non-empty q parameter is required.", "retryable": false}}
```

Codes: `bad_request` (400), `not_found` (404), `rate_limited` (429),
`provider_unavailable` (503), `database_error` (503), `internal` (500).

### `GET /v1/search`

```json
{
  "query": "bengaluru metro",
  "page": 1,
  "limit": 10,
  "total_estimate": 1,
  "took_ms": 4,
  "degraded": false,
  "cache_hit": true,
  "providers": {"eyesearch_index": 1},
  "failed_providers": [],
  "results": [
    {
      "id": "eyesearch:42",
      "title": "Bengaluru Metro Purple Line timings",
      "url": "https://english.bmrc.co.in/metro-timings",
      "snippet": "Purple Line services run 05:00 to 23:00 on weekdays.",
      "source": "eyesearch_index",
      "kind": "web",
      "poster_url": null,
      "thumbnail_url": null,
      "published_at": null,
      "score": 1.0
    }
  ]
}
```

Every field is either read from the index or `null`. The only derived value is
`thumbnail_url` for YouTube URLs, which is a pure function of the video id
(`i.ytimg.com/vi/<id>/hqdefault.jpg`) and therefore not a guess.

## The schema

`sites` holds one row per domain, `pages` one row per URL, and `page_fts` is
**external-content FTS5** over `pages`. Three triggers keep the index in step
with the table, so application code never writes to `page_fts` and a half-synced
row is impossible.

```sql
CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    canonical_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER,
    url TEXT UNIQUE NOT NULL,
    title TEXT,
    description TEXT,
    body_text TEXT,
    quality_score REAL DEFAULT 1.0,
    digest TEXT,
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(site_id) REFERENCES sites(site_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
    title, description, body_text,
    content='pages', content_rowid='page_id'
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    domain TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Plus `pages_ai` / `pages_ad` / `pages_au` triggers, a **partial unique index on
`digest`** (the database itself enforces SHA-256 deduplication), and indexes on
`pages(site_id)`, `pages(indexed_at)` and `crawl_queue(status, rowid)`.

The triggers are not decoration. Without them an external-content FTS5 table
holds no rows and every search returns nothing while every write reports
success. `tests/test_persistence_atomic.py::test_the_index_is_searchable_immediately_after_a_commit`
exists to keep that true.

## Hard invariants

1. **Two database requests per second, process-wide.**
   `ProcessRateLimiter(interval_seconds=0.5)` in `app/libsql_http.py` is shared
   by every caller — crawler, revalidation and request path alike. One lock, one
   timestamp, so the ceiling is a property of the process rather than of each
   worker's good intentions.

2. **Every Turso step is checked.** Turso answers HTTP 200 with a per-step error
   object. `TursoHttpClient` inspects `results[i].type` and raises with the
   failing statement attached:
   `libsql step 1 failed: SQLITE_CONSTRAINT: … (statement: INSERT INTO sites …)`.
   A write that did not happen can no longer look like one that did.

3. **Storage fails closed.** `StorageGuard` caches `COUNT(*)` for 60 seconds and
   is asked once per batch, never once per URL. Any exception while reading the
   count sets `paused=True` and blocks writes: an unknown page count is not
   permission to keep writing past a 300,000-page ceiling.

4. **Body text is capped at 1,000 characters**, cut at the nearest word boundary
   and closed with `…`. The ellipsis counts against the cap.

5. **The quality gate is strict.** At least 80 characters of body text, a title
   of at least 3 characters, no WAF interstitial, and SHA-256 deduplication on
   cleaned body text — enforced in the gate, in the batch, and by a unique index.

6. **Persistence is one trip and it is atomic.** A batch is
   `BEGIN IMMEDIATE → sites upsert → pages insert → crawl_queue UPDATE → counts → COMMIT`.
   If any statement fails, none of it lands and the URL is not marked processed.
   A fetch, parse or quality failure writes `status = 'failed'` immediately, so
   the queue moves on instead of retrying a dead URL forever.

7. **Counters move only on a confirmed commit.** `IndexOutcome.counted` is
   derived from `committed`, and the reported page total is read back from the
   database inside the same transaction that wrote the rows.

8. **Workers never block startup.** They are scheduled from a bootstrap task
   after the event loop has a turn, so the port binds and `/health` answers in
   milliseconds while the crawl runs behind it. A batch iteration sleeps 2.0 s;
   an empty queue backs off 15.0 s.

## Running it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload            # local SQLite, no setup required
```

Against Turso:

```bash
export TURSO_DB_URL=libsql://<db>-<org>.turso.io
export TURSO_AUTH_TOKEN=<token>
export ENVIRONMENT=production
```

`libsql://`, `ws://` and `wss://` are rewritten to `https://` on the way in,
because those schemes select a WebSocket backend that Turso's Hrana endpoint
rejects with a 400. Production refuses to boot on a `file:` URL unless
`ALLOW_LOCAL_DB=true` is set deliberately.

```bash
python -m app.indexer.crawler --probe     # exits non-zero if the DB rejects writes
python -m app.indexer.crawler --refill    # queue the seed URLs
python -m app.indexer.crawler --once      # run a single batch of 10
python -m app.indexer.crawler --stats     # index counters
```

## Tests

```bash
python -m pytest
```

Every test runs against an in-memory SQLite database through the same
`LibsqlClient` interface production uses, so the real SQL, triggers, FTS index
and atomic batches are under test. Nothing touches the network.

| file | what it pins down |
| --- | --- |
| `test_rate_limiter.py` | the 2 req/s ceiling, concurrency, scheme rewriting, per-step error surfacing |
| `test_parser_cap.py` | the 1,000-character cap, word-boundary cuts, tag stripping |
| `test_quality_gate.py` | the 80-character floor, junk markers, SHA-256 stability and dedupe |
| `test_persistence_atomic.py` | atomic batches, confirmed-commit counters, queue transitions, guard TTL and fail-closed |

## What is not here

No live provider fallback and no third-party snippets. The index is the only
source of an answer: if a query is not indexed, `/v1/search` returns no results
rather than borrowing someone else's. `/v1/chat` is extractive for the same
reason — it composes an answer from indexed text and cites the URLs, and there
is no model behind it.
