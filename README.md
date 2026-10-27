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
│   ├── 06_revalidation_agent.py Background stale-page revalidation
│   ├── 07_classifier_agent.py  Ten-category tagging at ingestion
│   └── 08_language_agent.py    Script detection: en / hi / kn
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
| GET | `/v1/search?q=&limit=&page=&category=&lang=` | result cards for the web UI |
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
    category TEXT DEFAULT 'general_reference',
    language TEXT DEFAULT 'en',
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

## Link discovery

The crawl is recursive: every page that parses feeds its own outbound links
back into `crawl_queue`, so the frontier refills instead of draining after the
seeds. The work is split in two, and the split matters:

- **`03_parser_agent.py` extracts.** `extract_links()` returns absolute URLs
  from `<a href>`, resolving relative hrefs against the page URL, stripping
  fragments, and dropping `mailto:`, `tel:`, `javascript:`, `data:` and `ftp:`
  hrefs. It returns at most `MAX_LINKS_PER_PAGE` (40) per page. It does **not**
  judge crawl scope.
- **`01_frontier_agent.py` decides.** `filter_links()` applies the one
  definition of crawl scope, and `normalise_url()` collapses variants of the
  same page:

| rule | value |
| --- | --- |
| scheme | `http` / `https` only |
| host | the source host, any `.in` domain, or a whitelisted Indian portal |
| path depth | ≤ `MAX_PATH_DEPTH` (6) — Indian portals nest real content several levels down |
| extensions | no `.png`, `.pdf`, `.zip`, `.css`, `.js`, media, fonts, archives |
| paths | no `/login`, `/cart`, `/search`, `/account`, `/wp-admin` … |
| query | tracking keys (`utm_*`, `gclid`, `fbclid`, `ref`, …) dropped, keys sorted |
| frontier size | capped at `MAX_PENDING_QUEUE` (10,000) pending rows |

Crawl scope is not limited to public suffixes: `WHITELISTED_DOMAINS` adds
Indian news, legal, startup and developer media that sit on `.com` — thehindu,
indianexpress, prajavani, vijaykarnataka, indiankanoon, livelaw, barandbench,
inc42, hasgeek — alongside `data.gov.in`, `freejobalert.com` and `ncs.gov.in`.
The seed list is 21 roots chosen to span categories rather than to favour
government alone.

`normalise_url()` also removes the fragment, the trailing slash and `www.`, so
`/about`, `/about/`, `/about#history` and `/about?utm_source=x` are one queue
row rather than four.

The orchestrator collects links from every page that parsed — a thin page can
still point at good ones — and enqueues the aggregate **after** the batch
commits, so a failed write never grows the frontier. `BatchOutcome.discovered`
reports how many were added.

Verified with a stubbed site: batch 1 queues 3 links, batch 2 queues 5, batch 3
indexes the rest and the frontier empties at the depth limit — 9 pages indexed,
9 queue rows `processed`, none stranded.

### Frontier re-arm — why the crawl does not stop at the seeds

Discovery alone is not enough. `enqueue()` rejects any URL already in `pages`
or `crawl_queue`, so once every seed has been crawled the seed list yields
nothing, the queue stays empty and the worker idles permanently. That is the
stall this section exists to prevent.

`FrontierAgent.refill()` therefore runs in two stages:

1. queue any seed not seen yet;
2. if nothing new was added, return the stalest crawled URLs to `pending`.

Stage 2 is bounded by two settings: `REARM_COOLDOWN_SECONDS` (6 h) so a URL is
not re-fetched on every pass, and `REARM_BATCH_SIZE` (25) so the frontier
refills gradually. `crawl_queue.processed_at` records when a URL was finished;
it is added to an existing table by `ensure_columns()` in `app/database.py`,
which is how a column introduced after a table was first created gets applied
without a rebuild.

When the frontier is dry and nothing is past its cooldown, the worker backs off
60 s instead of 15 s, so a six-hour cooldown costs 360 wake-ups rather than
1,440.

## Per-domain page cap

One large portal must not crowd out the rest of the index. `MAX_PAGES_PER_DOMAIN`
(10 by default) is enforced in `Orchestrator.run_batch`:

- the counts for every domain in the batch are read in **one** query —
  `SELECT s.domain, COUNT(*) FROM pages p JOIN sites s ON s.site_id = p.site_id
  WHERE s.domain IN (…) GROUP BY s.domain` — never one per URL;
- URLs on a domain that is already full are parked as `skipped` **before** any
  fetch, so a capped domain costs no network request;
- the count is incremented as pages are accepted, so a domain that fills up
  part-way through a batch stops at the cap rather than overshooting;
- discovered links for a capped domain are not added to the frontier at all.

`skipped` is deliberately distinct from `failed`: it is a policy decision, not
an error, and `rearm_processed()` never revives it. If you raise the cap later,
those rows stay skipped — clear them deliberately if you want them retried.

Note that `pages` has no `domain` column; the domain is reached through
`sites`, which is why the count is a join rather than a filter on `pages`.

## Page-specific descriptions

Portals ship one boilerplate meta description across hundreds of subpages, so
every result card read identically. `build_description()` in
`03_parser_agent.py` keeps a meta description only when it can distinguish the
page. It is treated as generic when it is:

- shorter than 20 characters,
- opening with boilerplate (`official portal`, `official website`, `welcome to`,
  `home page`, `government of india`, …), or
- identical to the page title.

Otherwise the description becomes `<title> — <first 180 chars of body>`, cut at
a word boundary and capped at 400 characters. The ellipsis is appended only
when text was actually removed, so a short page does not advertise a truncation
that never happened.

Measured on a stubbed portal whose every subpage carried the same meta
description: 10 pages indexed, 10 distinct descriptions.

## Ranking and match snippets

`GET /v1/search` ranks with bm25 weighted `title 10, description 5, body 1`, plus
an explicit title boost: a row whose title contains any of up to five query
terms has `TITLE_MATCH_BOOST` subtracted from its bm25 score. bm25 is negative
and lower-is-better, so that promotes the row.

`score` in the response is **relative within the page of results** — the best
hit is `1.0` and the rest are spread below it. That is deliberate: the boost
pushes a good row below zero, and mapping bm25 absolutely would make a boosted
result report a *worse* score than an unboosted one. Scores from different
pages are not comparable.

The snippet shown for each result is the passage of the body around the first
query term, cut at word boundaries and capped at 320 characters, with an
ellipsis only where text was removed. When the match was on the title alone the
snippet falls back to the description rather than repeating the heading.

**Why not FTS5's `snippet()`.** The brief asked for it and it was the obvious
choice, but on the SQLite this project runs on, `snippet()` ignores its
`colnum` argument and returns column 0 for every call — reproduced against both
a plain and an external-content FTS5 table, where `highlight()` behaves
correctly and `snippet()` does not. Turso runs a different SQLite that cannot
be exercised from here, so the window is computed in Python
(`passage_around_match`) instead: portable, deterministic and covered by tests.

## Classification and language

Both agents run at ingestion, so a query can be narrowed without a backfill of
pages that were crawled before the feature existed.

**`07_classifier_agent.py`** assigns one of ten categories, scored from three
signals in descending order of trust:

| signal | weight | why |
| --- | --- | --- |
| public suffix (`.gov.in`, `.ac.in`, `.edu.in` …) | 4 | structural — true whatever the copy says |
| URL token (`recruitment`, `mandi`, `judgment` …) | 3 | the publisher's own label |
| keyword in the title or description | 2 | deliberate emphasis |
| keyword in the body | 1, max 3 per category | a footer mention is not a topic |

Categories: `government`, `education_exams`, `news_media`, `jobs_careers`,
`legal_judiciary`, `business_finance`, `tech_developer`, `health_medical`,
`agriculture_rural`, and `general_reference` as the fallback.

URL tokens are matched as **whole words**, not substrings — `example.com`
contains `exam`, and substring matching classified a plain "About us" page as
an exam page. A suffix alone (weight 4) beats a single URL token but loses to a
URL token plus a title keyword, which is what keeps `ssc.nic.in/admit-card` in
`education_exams` rather than `government`.

**`08_language_agent.py`** detects `en`, `hi` or `kn` from Unicode blocks —
Kannada `U+0C80–U+0CFF`, Devanagari `U+0900–U+097F` — rather than word lists. A
script must carry at least 15 % of the letters and at least 3 characters before
it wins, so an English page with a Kannada menu is not labelled Kannada.

Both columns are added to `pages` by `ensure_columns()` on an existing
database, and indexed for filtering.

### Filtering

```bash
curl 'localhost:8000/v1/search?q=metro&category=government&lang=kn'
```

`category` and `lang` are optional and validated: an unknown value returns
`400 bad_request` with the usual envelope rather than an empty result set that
looks like a miss. The response echoes what was applied:

```json
{"filters": {"category": "government", "language": "kn"}}
```

`format=json` honours the same filters. Revalidation refreshes a page's content
but deliberately leaves `category` and `language` alone — otherwise every
refreshed page would silently reset to the defaults.

## Upgrading an existing database

The rebuild uses the same table names as the previous backend — `sites`,
`pages`, `crawl_queue` — with different columns. `CREATE TABLE IF NOT EXISTS`
cannot change a table that already exists, so a warm database would keep the old
shape and every write would fail on a missing column, once per crawl batch.

`app/database.py` therefore checks the schema before the DDL batch runs:

- a `pages` table without `body_text`, or any of `documents`, `documents_fts`,
  `search_records`, `crawl_backlog`, means the database predates this schema;
- with `MIGRATE_LEGACY_SCHEMA=true` (the default) those tables are dropped in one
  transaction and the new schema is created — logged at WARNING with exactly
  what was removed;
- with `MIGRATE_LEGACY_SCHEMA=false` the app refuses to boot and names the
  tables it found, so nothing is dropped by surprise.

The dropped rows are a crawl cache in a shape this code cannot read, so they are
cleared rather than migrated. If those pages matter, point the app at an empty
database and let the crawler refill it.

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
| `test_link_discovery.py` | link extraction, crawl scope, frontier re-arm |
| `test_domain_cap_and_description.py` | per-domain cap, page-specific descriptions |
| `test_classifier_and_language.py` | category and language tagging, search filters |
| `test_search_ranking.py` | match snippets, title boost, score ordering |

## What is not here

No live provider fallback and no third-party snippets. The index is the only
source of an answer: if a query is not indexed, `/v1/search` returns no results
rather than borrowing someone else's. `/v1/chat` is extractive for the same
reason — it composes an answer from indexed text and cites the URLs, and there
is no model behind it.
