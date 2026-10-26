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

## Which database is this process actually using?

A gateway that silently falls back to a local SQLite file looks healthy — search
still answers from the live providers — while the cloud database records no
reads and no writes. That failure mode is now impossible to miss:

- **On startup** the gateway logs the target and proves it with a real query:

  ```
  Connected to Remote Turso DB: libsql://eyesearch-<org>.turso.io
  database ready (remote): libsql://eyesearch-<org>.turso.io
  index: 12 documents, 0 queued queries (libsql://eyesearch-<org>.turso.io)
  ```

  A local file logs a warning instead: `Connected to LOCAL SQLite: file:./local.db
  — this is not the Turso cloud database`.

- **`/health`** reports `db_target` (`remote:<url>` or `local:<path>`) next to
  `db: up|down` and the index counters, so the deployment can be checked from
  outside without opening the Turso dashboard.

- **A remote URL without a token is refused**, with the fix in the message:
  `TURSO_AUTH_TOKEN is required for the remote database libsql://…`.

- **Production refuses to boot on a local file.** With `ENVIRONMENT=production`
  and a `file:` URL the process exits with `refusing to start: … set
  TURSO_DB_URL/TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or set ALLOW_LOCAL_DB=true
  if that is genuinely intended`. A database that is unreachable in production is
  fatal for the same reason: booting anyway means every write is lost.

Required environment on Render: `TURSO_DB_URL` (or `TURSO_DATABASE_URL`) =
`libsql://<db>-<org>.turso.io`, and `TURSO_AUTH_TOKEN` = the database token.

**Scheme handling.** Turso's cloud speaks Hrana over HTTPS, but `libsql_client`
picks its backend from the URL scheme — `libsql://`, `wss://` and `ws://` all
select the WebSocket client, which Turso rejects with `400, message='Invalid
response status'`. Remote URLs are therefore rewritten on the way in
(`libsql://` → `https://`, `wss://` → `https://`, `ws://` → `http://`), which
selects `HttpClient`, and the token is passed as `Authorization: Bearer …`. The
startup log names the client backend (`Connected to Remote Turso DB: … (HttpClient)`)
and logs the rewrite, so a WebSocket connection can never go unnoticed again.
`file:` URLs are left untouched.

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

**Result cache keying** — `search_records` is keyed by
`UNIQUE (query_key, url)` where `query_key = sha256(" ".join(q.split()).lower())[:40]`.
Two queries can cache the same URL without one taking it from the other, and a
query can only ever read rows written for itself — the lookup is
`WHERE query_key = ?`, never a shared or partial key. A pre-existing table with
the old `url UNIQUE` constraint is dropped and recreated on startup (it is a
cache, and dropping it also clears rows poisoned by the old matching bug).

**Schema — two tiers, rebuilt on startup.** `sites` holds one row per domain,
`pages` holds a bounded set of rows per site (the root plus its sitelinks), and
`page_fts` is external-content FTS5 over the pages:

```sql
CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    authority_score REAL NOT NULL DEFAULT 1.0,
    last_crawled_at INTEGER
);

CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('root', 'sitelink')),
    title TEXT NOT NULL DEFAULT '', headings TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', nav_terms TEXT NOT NULL DEFAULT '',
    site_terms TEXT NOT NULL DEFAULT '', category_terms TEXT NOT NULL DEFAULT '',
    url_terms TEXT NOT NULL DEFAULT '', http_status INTEGER NOT NULL DEFAULT 200,
    noindex INTEGER NOT NULL DEFAULT 0, canonical_url TEXT, content_hash TEXT,
    last_crawled_at INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
    title, headings, description, nav_terms, site_terms, category_terms, url_terms,
    content='pages', content_rowid='page_id',
    tokenize='unicode61 remove_diacritics 2', detail=full
);
```

Three triggers (`pages_ai`, `pages_ad`, `pages_au`) keep the index in step with
`pages`, so application code never writes to `page_fts`. On startup the legacy
flat model is dropped (`documents`, `documents_fts`, `search_records`) and the
gateway logs what it found: `schema ready on remote: 12 sites, 74 pages, 74 indexed`.
`crawl_backlog` and `crawl_queue` carry the crawler's work.

**Retrieval** — the weighted query from the brief, one card per site:

```sql
SELECT p.page_id, p.site_id, s.domain, s.name, p.url, p.title, p.description, p.kind,
       bm25(page_fts, 10.0, 5.0, 2.5, 3.0, 4.0, 1.0, 2.0) AS lexical_score
FROM page_fts
JOIN pages p ON p.page_id = page_fts.rowid
JOIN sites s ON s.site_id = p.site_id
WHERE page_fts MATCH ? AND p.http_status = 200 AND p.noindex = 0
ORDER BY lexical_score ASC LIMIT 50;
```

Matched pages are grouped by site: the root page becomes the card and the site's
sitelink pages are attached as `sitelinks`. A query that matches a sitelink
directly ("ztube pricing", "documentation") returns the parent site card with
that sitelink promoted to the front and marked `"matched": true` — independent
sitelink search without a separate result row per path.

Measured on a local database: 4–5 ms warm for a grouped query, 19 ms on the
first call after a write.

**Category-seed crawler** (`app/indexer/crawler.py`) — our own fleet, seeded by
category, never a third-party snippet dump:

| category | seeds |
| --- | --- |
| government | `india.gov.in`, `karnataka.gov.in`, `bengaluruurban.nic.in`, `ksrtc.in`, BMRC |
| banking | `rbi.org.in`, `sbi.co.in`, `hdfcbank.com`, `icicibank.com`, `npci.org.in` |
| sports | `iplt20.com`, `bcci.tv`, `prokabaddi.com`, `espncricinfo.com` |
| e-commerce | `flipkart.com`, `myntra.com`, `nykaa.com`, `irctc.co.in`, `amazon.in` |
| education | `ugc.gov.in`, `iitb.ac.in`, `iisc.ac.in`, `nta.ac.in`, `aicte-india.org` |
| software | `github.com`, `python.org`, `developer.mozilla.org`, `nodejs.org`, `kotlinlang.org` |
| knowledge | main-namespace `en.wikipedia.org/wiki/<Article>` (Talk/Special/User/File/Category rejected) |

The registry (`app/indexer/seed_registry.py`) holds **211 authoritative Indian
roots** across government (central + every state and UT), public utilities and
transit, universities/IITs/NITs/IIMs and exam boards, regulators and courts, and
national digital services — plus banking, sports, e-commerce and software.
Domains are crawled with `SEED_CRAWL_CONCURRENCY` (5) in flight, one shared HTTP
client and the single Turso connection, and every domain is stored as **one root
page plus at most five sitelinks** inside the 10 KB domain budget.

**The crawl is an audit, not a download.** Each seed root is fetched as desktop
Chrome — the plain `Mozilla/5.0 … Chrome/128.0.0.0 Safari/537.36` agent plus the
matching `Accept`, `Accept-Language`, `Sec-Fetch-*` and
`Upgrade-Insecure-Requests` headers — with redirects followed and a 15 s timeout
(`CRAWL_USER_AGENT`, `CRAWL_TIMEOUT_SECONDS`). A bot token in the agent is what
earns a 403/406 from several government WAFs, so there is none. TLS
follows `CRAWL_VERIFY_TLS`: `auto` (default) verifies and, when a host presents a
broken chain — common on NIC portals — retries that one request unverified with a
warning; `never` skips verification outright; `always` refuses the fallback.
Title, description, H1/H2 headings and up to five authoritative child links are
extracted, and the domain is committed straight to `sites`/`pages` — no
intermediate holding pen.

**Every stage is logged, so "no writes" is diagnosable from the log alone.**
Startup prints `RUNNING ENGINE COMMIT: <sha> (RENDER_GIT_COMMIT)` so you can see
which build is live. Each seed logs `SEED START: processing domain …` and
`SEED FETCHED: domain … returned status …`; each write logs
`PREPARING DB WRITE: domain … with N pages`; each commit logs
`DB COMMIT SUCCESS: Indexed … (Running total: N)`. Every request to the Turso
pipeline logs its statements — `TURSO PIPELINE EXECUTE: ['SELECT domain FROM
sites …', 'INSERT INTO sites …']` — with a running tally every fifty statements
(`TURSO STATEMENTS SO FAR: {'INSERT': 12, 'SELECT': 480}`), which is how you tell
a read loop from an ingestion loop.

**Schema migrations run before the DDL batch, and are verified.** A production
database whose `crawl_backlog` predated the `kind` column made every backlog
insert fail (`table crawl_backlog has no column named kind`), and the index over
that column inside the base batch aborted the whole schema initialisation. Now:
existing tables are migrated *before* the batch, the index is created only once
the column exists, every `ALTER` failure is logged at ERROR, and the result is
verified afterwards (`migration applied: crawl_backlog.kind`). If a column still
cannot be added, the backlog statements fall back to their legacy column set, so
queries keep queueing instead of throwing.

**Commits are isolated.** `sites` are written in their own pipeline request and
logged (`SITES COMMITTED: N site rows written`) before pages are attempted, and
page batches run non-strict: a failing step is logged and counted
(`failed_statements`) without discarding the statements that did commit. A
backlog write can never roll back a site or page commit.

**Startup proves the database is writable.** Before any crawling,
`verify_database()` logs the exact integers Turso returns
(`SELECT count(*) FROM sites = 71, pages = 214, page_fts = 214`), then inserts a
sentinel row, reads it back and removes it. A read-only token, the wrong branch
or a staging database fails here, loudly, instead of silently during a crawl —
`GET /v1/stats` reports the same verdict as `"writable": true|false`. To run the
check on its own:

```bash
python -m app.indexer.crawler --probe   # exits non-zero when the DB rejects writes
```

**Turso answers 200 OK even when a statement failed.** The pipeline client
inspects every step in `results` and raises with the statement text
(`libsql step 1 failed: SQLITE_CONSTRAINT: UNIQUE constraint failed: sites.domain
(statement: INSERT INTO sites …)`), so an HTTP 200 can no longer hide a failed
write.

**Every seed persists a root — whatever the server answers.** A 200 with
nothing extractable, a JavaScript shell, a 403, a 406, a 500 or a connection/SSL
failure all end in a committed root record, titled with the seed label when we
have content and with the bare hostname when we do not (`eci.gov.in`). Sitelinks
are optional; only a syntactically invalid domain is skipped. Each seed is
written as it finishes, so the site count moves with every seed rather than at
the end of the pass. Verified against all five cases in one run: `eci.gov.in`
(403), `ssc.nic.in` (406), `sslbroken.gov.in` (SSL failure), `error.gov.in`
(500) → hostname roots, `karnataka.gov.in` (200) → extracted title; 5 seeds,
5 sites, 5 pages committed.

**Nothing is skipped silently.** Every page the crawler rejects is logged with
the rule that rejected it — `skipped: no_title`, `skipped: blocked_status`,
`skipped: not_indexable`, `skipped: low_text_density`, `skipped: fetch_failed`,
`skipped: robots_disallowed` — and the per-seed summary carries the tally
(`produced no indexable pages (fetched 1, skipped 1: no_title=1)`). A successful
commit logs `DB COMMIT SUCCESS: Indexed <domain> with N pages (Running total: M)`,
where M is read back from the database, so the line is proof of the commit and
not just of the attempt.

The extraction gate was also too strict for government homepages: it required
80 characters of server-rendered body text, which threw away JavaScript shells
that carry a real title and meta description. Since the schema stores no page
body, a page is now accepted when it has an identity and something to match on —
title, description, headings or navigation vocabulary — and rejected only when
it has none of them.

`GET /v1/stats` returns `sites`, `pages`, `indexed`, `query_backlog`,
`discovered_backlog` and `max_payload_bytes`, read from the database on every
call with no caching anywhere.

**Writes land per domain, not per pass.** Each seed's pages are written the
moment that seed finishes (`asyncio.as_completed`), so a long pass no longer
holds every write until the end — on a free instance that used to mean a restart
mid-crawl lost the whole pass while the logs still showed HTTP 200s and
discovery lines. Every successful write logs
`Successfully indexed site: <domain> (pages: N, total sites: M)`, a failed write
logs the exception type, message and statement, and a write that stores nothing
logs exactly which guard rejected the rows.

The discovered-domain queue drains in its own worker every
`DISCOVERY_INTERVAL_SECONDS` (300 s), independent of the seed pass, so backlog
items are promoted into `sites`/`pages` even while a long seed run is in flight.

**The Turso client is ours.** `libsql_client`'s HTTP backend parses the response
as `response["result"]`, but Turso's pipeline endpoint answers
`{"results": [{"type": "ok", "response": {"type": "execute", "result": {...}}}]}`.
That mismatch raised `KeyError: 'result'` on every statement, so reads and
writes to `crawl_backlog` silently returned nothing. `app/libsql_http.py` now
speaks the v2 pipeline directly and accepts every shape we have seen —
`results` lists, a single `result`, plain `cols`/`rows` mappings, and Hrana step
objects — decoding typed cells (`text`, `integer`, `float`, `null`, `blob`) into
Python values. An `INSERT` with no rowset is success, not an error, and an empty
pipeline is "no rows". `execute_safe()` still wraps every call and retries once
on a malformed response.

### Continuous discovery

Every page the crawler reads is also mined for outbound links. A host joins the
backlog when it looks like an Indian authority — a `.gov.in`, `.nic.in`,
`.ac.in`, `.res.in`, `.edu.in`, `.org.in`, `.co.in` or `.in` domain — and is
rejected when it is a social network, ad network, CDN, shortener, login
redirector or non-Indian commercial site (`app/indexer/discovery.py`). Hosts
already in `sites` are never queued again.

The backlog drains in **batches of 10 domains** with a 0.5 s pause between
batches (`DISCOVERY_BATCH_SIZE`, `DISCOVERY_BATCH_PAUSE_SECONDS`), and stops at
`MAX_INDEXED_SITES` (50,000) or when the queue empties. Measured on a cold local
database with no manual command: boot in **608 ms**, `/health` in **11 ms** while
crawling, and after four minutes **113 sites / 269 pages** (113 root + 156
sitelink) with **480 domains discovered** and the queue still draining.

A pass crawls each seed's entry page and follows same-host **level-1 links only**,
storing one root page plus at most five sitelinks per domain with
`INSERT OR REPLACE` so a re-crawl refreshes rather than duplicates.

**Nothing here needs a human.** On boot the lifespan checks coverage and, when
the index is under `SEED_COVERAGE_MIN_SITES` (5), `SEED_COVERAGE_MIN_RATIO` (1.0), `DISCOVERY_BATCH_SIZE` (10),
`DISCOVERY_BATCH_PAUSE_SECONDS` (0.5), `DISCOVERY_INTERVAL_SECONDS` (300), `MAX_INDEXED_SITES` (50000), launches
`run_autonomous_seed_crawl()` with `asyncio.create_task`, crawling **only the
missing seeds** so repeated boots make progress instead of repeating work. The
port binds and `/health` answers in milliseconds while the crawl runs behind it. The same
function wakes every `SEED_CRAWL_INTERVAL_SECONDS` (24 h) to re-verify the seeds
and pick up new authoritative links, and it never raises out of the loop: a dead
domain is logged and skipped, and the pass carries on.

Measured on a cold local database with no manual command: lifespan returned in
**300 ms**, `/health` answered **200 in 12 ms** while the crawl was in flight,
and by **+20 s** the index held 5 sites / 26 pages (5 root + 21 sitelink), all
26 in `page_fts` — `bank` → RBI in 15 ms, `karnataka` → bengaluruurban.nic.in in
4 ms, `india` → 3 hits.

To run it by hand (optional, not required):

```bash
python -m app.indexer.crawler --list
python -m app.indexer.crawler                      # the five India-first categories
python -m app.indexer.crawler --category banking
python -m app.indexer.crawler --verify             # sites, pages, sitelinks, sample searches
```

**Depth and budget rules.** Root and level-1 pages only
(`INDEX_MAX_PATH_DEPTH=1`), with main-namespace Wikipedia articles as the
documented exception — `/wiki/<Article>` *is* their entry-point shape. Each
document stores the clean canonical title, the normalised URL, the domain, the
category and a **3–4 sentence factual summary** capped at 1.5 KB
(`INDEX_SUMMARY_SENTENCES`, `INDEX_CONTENT_MAX_CHARS`), inside a row budget of
1.5 KB (`INDEX_MAX_PAYLOAD_BYTES`), sitelinks included. Scripts, styles,
navigation, cookie notices, footers, ads and inline images never reach storage.

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

**Routing** (`app/services/search_service.py`) — index first, verified extraction on a miss:

```
documents_fts  →  any hit  →  serve immediately (source: "index", no outbound call)
              →  miss      →  providers supply *candidate URLs only*
                             → universal relevance gate (≥ 60% of query words in title/URL/snippet)
                             → native extractor fetches and parses the survivors
                             → gate again on our own extract
                             → upsert into documents + documents_fts
                             → serve our extracts (source: "index")
                             → nothing verified: source "queued", query goes to the crawler
```

Provider titles and snippets are **never served and never stored**. The provider
tells us which URLs might answer the query; what the user sees is our own
extract of that page — title, URL, snippet and favicon from `documents`, capped
at the same 2 KB budget. That is what stops unverified provider dumps from
becoming results.

Verified through the API with three candidates for one query:

| step | outcome |
| --- | --- |
| gate | 3 candidates in, weather page dropped, 2 pass |
| request 1 | `eyesearch_index`, 2 documents extracted and stored, 1 provider call |
| request 2 | `eyesearch_index`, 19 ms, no provider call |
| request 3 | `eyesearch_index`, 4 ms |
| `search_records` | 0 rows — nothing is dumped there any more |

`auto_ingested` reports how many documents that request wrote. A query whose
candidates all fail the gate is queued for the crawler, and a repeat within ten
minutes is answered locally rather than retried. `LIVE_SEARCH_ENABLED=false`
gives a strictly local deployment.

### The universal relevance gate

One rule, no special cases — `app/engine/relevance.py`:

- the query is tokenised (Latin, Devanagari and Kannada), lower-cased, stopworded;
- a candidate passes when **≥ 60 %** of those words appear in its title, URL or
  snippet (`RELEVANCE_MIN_TOKEN_COVERAGE`);
- prefix matching absorbs simple stemming ("bengal" ↔ "bengaluru");
- a word that appears only in the page body does not count, which is what stops
  a passing footer mention from being served as an answer.

The same function gates index hits and freshly discovered candidates, so there
is exactly one definition of "relevant" in the system.

### Domain root compaction and sitelinks

One row per domain root, not one per page. A sub-page of a domain that already
has a root entry is **folded into it**: its high-value navigation targets are
merged into the root's `sections_json` and the row is not stored
(`{"collapsed": 1, "inserted": 0}`), so Turso never fills with redundant path
rows.

```sql
sections_json = '[{"title":"Sign in","url":"https://ztube.com/login","snippet":"Access your account"},
                  {"title":"Documentation","url":"https://ztube.com/docs","snippet":"Official developer guides"}]'
```

Sections are extracted generically from the page's own links — sign in, sign up,
documentation, pricing, dashboard, download, support — capped at
`INDEX_SECTIONS_MAX` (5), de-duplicated by URL, and counted inside the row's
1.5 KB budget.

Serving unpacks them into sub-cards:

- `GET /v1/search?q=ztube` → the root card plus five `sitelinks`, none marked.
- `GET /v1/search?q=ztube pricing` → the **Pricing** sitelink is promoted to the
  front and marked `"matched": true`, and the parent card's score is boosted
  (0.8 → 1.0) so the intended destination leads.

Section names are also folded into the document's searchable text, and a
document's sections count toward the relevance gate — so a query for a section
matches its domain root instead of falling through to discovery. The flat
`format=json` contract carries the same `sitelinks` array.

### Native extraction (`app/indexer/extractor.py`)

`extract_candidate()` fetches one URL (robots.txt honoured, byte-capped) and runs
it through the extractor; `extract_candidates()` does up to
`ON_DEMAND_MAX_FETCHES` (5) with bounded concurrency. The extractor strips
scripts, styles, navigation, footers, ads, inline images and base64 payloads,
keeps the title (≤ 100 chars), snippet (≤ 200) and a short lead, and fits the
whole row to the 2 KB budget. Ingestion uses `mode="upsert"`, so a re-verified
page replaces its row in place rather than duplicating it; the crawler keeps its
stricter insert-only rule.

### Result cache — removed

`search_records` no longer exists. The two-tier index answers every query, so a
second cache keyed by query string had nothing left to do; it is dropped at
startup along with the flat `documents` tables.

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

**Result cache keying** — `search_records` is keyed by
`UNIQUE (query_key, url)` where `query_key = sha256(" ".join(q.split()).lower())[:40]`.
Two queries can cache the same URL without one taking it from the other, and a
query can only ever read rows written for itself — the lookup is
`WHERE query_key = ?`, never a shared or partial key. A pre-existing table with
the old `url UNIQUE` constraint is dropped and recreated on startup (it is a
cache, and dropping it also clears rows poisoned by the old matching bug).

**Schema — rebuilt automatically on startup.** Every table is
`CREATE ... IF NOT EXISTS`, so a cold start against an empty Turso database
recreates the whole thing with no manual step:

```sql
CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY, title TEXT, snippet TEXT, content TEXT,
    domain TEXT, category TEXT, created_at TIMESTAMP, ...   -- + operational columns
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, snippet, content, url,
    content='documents', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
```

`documents_fts` is **external content**: the text is not stored twice, and three
triggers (`documents_ai`, `documents_ad`, `documents_au`) keep the index in
perfect sync — application code never writes to the FTS table directly, so a
half-synced row is impossible. On startup the gateway logs what it found
(`schema ready on remote: 18 documents, 18 indexed`), and an FTS table from an
older release (no external content, no triggers) is detected, dropped and
rebuilt from `documents` in one step. `crawl_backlog` and `crawl_queue` carry the
crawler's work, the latter tagged with the category it belongs to.

**Category-seed crawler** (`app/indexer/crawler.py`) — our own fleet, seeded by
category, never a third-party snippet dump:

| category | seeds |
| --- | --- |
| government | `india.gov.in`, `karnataka.gov.in`, `bengaluruurban.nic.in`, `ksrtc.in`, BMRC |
| banking | `rbi.org.in`, `sbi.co.in`, `hdfcbank.com`, `icicibank.com`, `npci.org.in` |
| sports | `iplt20.com`, `bcci.tv`, `prokabaddi.com`, `espncricinfo.com` |
| e-commerce | `flipkart.com`, `myntra.com`, `nykaa.com`, `irctc.co.in`, `amazon.in` |
| education | `ugc.gov.in`, `iitb.ac.in`, `iisc.ac.in`, `nta.ac.in`, `aicte-india.org` |
| software | `github.com`, `python.org`, `developer.mozilla.org`, `nodejs.org`, `kotlinlang.org` |
| knowledge | main-namespace `en.wikipedia.org/wiki/<Article>` (Talk/Special/User/File/Category rejected) |

The registry (`app/indexer/seed_registry.py`) holds **211 authoritative Indian
roots** across government (central + every state and UT), public utilities and
transit, universities/IITs/NITs/IIMs and exam boards, regulators and courts, and
national digital services — plus banking, sports, e-commerce and software.
Domains are crawled with `SEED_CRAWL_CONCURRENCY` (5) in flight, one shared HTTP
client and the single Turso connection, and every domain is stored as **one root
page plus at most five sitelinks** inside the 10 KB domain budget.

**The crawl is an audit, not a download.** Each seed root is fetched as desktop
Chrome — the plain `Mozilla/5.0 … Chrome/128.0.0.0 Safari/537.36` agent plus the
matching `Accept`, `Accept-Language`, `Sec-Fetch-*` and
`Upgrade-Insecure-Requests` headers — with redirects followed and a 15 s timeout
(`CRAWL_USER_AGENT`, `CRAWL_TIMEOUT_SECONDS`). A bot token in the agent is what
earns a 403/406 from several government WAFs, so there is none. TLS
follows `CRAWL_VERIFY_TLS`: `auto` (default) verifies and, when a host presents a
broken chain — common on NIC portals — retries that one request unverified with a
warning; `never` skips verification outright; `always` refuses the fallback.
Title, description, H1/H2 headings and up to five authoritative child links are
extracted, and the domain is committed straight to `sites`/`pages` — no
intermediate holding pen.

**Every stage is logged, so "no writes" is diagnosable from the log alone.**
Startup prints `RUNNING ENGINE COMMIT: <sha> (RENDER_GIT_COMMIT)` so you can see
which build is live. Each seed logs `SEED START: processing domain …` and
`SEED FETCHED: domain … returned status …`; each write logs
`PREPARING DB WRITE: domain … with N pages`; each commit logs
`DB COMMIT SUCCESS: Indexed … (Running total: N)`. Every request to the Turso
pipeline logs its statements — `TURSO PIPELINE EXECUTE: ['SELECT domain FROM
sites …', 'INSERT INTO sites …']` — with a running tally every fifty statements
(`TURSO STATEMENTS SO FAR: {'INSERT': 12, 'SELECT': 480}`), which is how you tell
a read loop from an ingestion loop.

**Schema migrations run before the DDL batch, and are verified.** A production
database whose `crawl_backlog` predated the `kind` column made every backlog
insert fail (`table crawl_backlog has no column named kind`), and the index over
that column inside the base batch aborted the whole schema initialisation. Now:
existing tables are migrated *before* the batch, the index is created only once
the column exists, every `ALTER` failure is logged at ERROR, and the result is
verified afterwards (`migration applied: crawl_backlog.kind`). If a column still
cannot be added, the backlog statements fall back to their legacy column set, so
queries keep queueing instead of throwing.

**Commits are isolated.** `sites` are written in their own pipeline request and
logged (`SITES COMMITTED: N site rows written`) before pages are attempted, and
page batches run non-strict: a failing step is logged and counted
(`failed_statements`) without discarding the statements that did commit. A
backlog write can never roll back a site or page commit.

**Startup proves the database is writable.** Before any crawling,
`verify_database()` logs the exact integers Turso returns
(`SELECT count(*) FROM sites = 71, pages = 214, page_fts = 214`), then inserts a
sentinel row, reads it back and removes it. A read-only token, the wrong branch
or a staging database fails here, loudly, instead of silently during a crawl —
`GET /v1/stats` reports the same verdict as `"writable": true|false`. To run the
check on its own:

```bash
python -m app.indexer.crawler --probe   # exits non-zero when the DB rejects writes
```

**Turso answers 200 OK even when a statement failed.** The pipeline client
inspects every step in `results` and raises with the statement text
(`libsql step 1 failed: SQLITE_CONSTRAINT: UNIQUE constraint failed: sites.domain
(statement: INSERT INTO sites …)`), so an HTTP 200 can no longer hide a failed
write.

**Every seed persists a root — whatever the server answers.** A 200 with
nothing extractable, a JavaScript shell, a 403, a 406, a 500 or a connection/SSL
failure all end in a committed root record, titled with the seed label when we
have content and with the bare hostname when we do not (`eci.gov.in`). Sitelinks
are optional; only a syntactically invalid domain is skipped. Each seed is
written as it finishes, so the site count moves with every seed rather than at
the end of the pass. Verified against all five cases in one run: `eci.gov.in`
(403), `ssc.nic.in` (406), `sslbroken.gov.in` (SSL failure), `error.gov.in`
(500) → hostname roots, `karnataka.gov.in` (200) → extracted title; 5 seeds,
5 sites, 5 pages committed.

**Nothing is skipped silently.** Every page the crawler rejects is logged with
the rule that rejected it — `skipped: no_title`, `skipped: blocked_status`,
`skipped: not_indexable`, `skipped: low_text_density`, `skipped: fetch_failed`,
`skipped: robots_disallowed` — and the per-seed summary carries the tally
(`produced no indexable pages (fetched 1, skipped 1: no_title=1)`). A successful
commit logs `DB COMMIT SUCCESS: Indexed <domain> with N pages (Running total: M)`,
where M is read back from the database, so the line is proof of the commit and
not just of the attempt.

The extraction gate was also too strict for government homepages: it required
80 characters of server-rendered body text, which threw away JavaScript shells
that carry a real title and meta description. Since the schema stores no page
body, a page is now accepted when it has an identity and something to match on —
title, description, headings or navigation vocabulary — and rejected only when
it has none of them.

`GET /v1/stats` returns `sites`, `pages`, `indexed`, `query_backlog`,
`discovered_backlog` and `max_payload_bytes`, read from the database on every
call with no caching anywhere.

**Writes land per domain, not per pass.** Each seed's pages are written the
moment that seed finishes (`asyncio.as_completed`), so a long pass no longer
holds every write until the end — on a free instance that used to mean a restart
mid-crawl lost the whole pass while the logs still showed HTTP 200s and
discovery lines. Every successful write logs
`Successfully indexed site: <domain> (pages: N, total sites: M)`, a failed write
logs the exception type, message and statement, and a write that stores nothing
logs exactly which guard rejected the rows.

The discovered-domain queue drains in its own worker every
`DISCOVERY_INTERVAL_SECONDS` (300 s), independent of the seed pass, so backlog
items are promoted into `sites`/`pages` even while a long seed run is in flight.

**The Turso client is ours.** `libsql_client`'s HTTP backend parses the response
as `response["result"]`, but Turso's pipeline endpoint answers
`{"results": [{"type": "ok", "response": {"type": "execute", "result": {...}}}]}`.
That mismatch raised `KeyError: 'result'` on every statement, so reads and
writes to `crawl_backlog` silently returned nothing. `app/libsql_http.py` now
speaks the v2 pipeline directly and accepts every shape we have seen —
`results` lists, a single `result`, plain `cols`/`rows` mappings, and Hrana step
objects — decoding typed cells (`text`, `integer`, `float`, `null`, `blob`) into
Python values. An `INSERT` with no rowset is success, not an error, and an empty
pipeline is "no rows". `execute_safe()` still wraps every call and retries once
on a malformed response.

### Continuous discovery

Every page the crawler reads is also mined for outbound links. A host joins the
backlog when it looks like an Indian authority — a `.gov.in`, `.nic.in`,
`.ac.in`, `.res.in`, `.edu.in`, `.org.in`, `.co.in` or `.in` domain — and is
rejected when it is a social network, ad network, CDN, shortener, login
redirector or non-Indian commercial site (`app/indexer/discovery.py`). Hosts
already in `sites` are never queued again.

The backlog drains in **batches of 10 domains** with a 0.5 s pause between
batches (`DISCOVERY_BATCH_SIZE`, `DISCOVERY_BATCH_PAUSE_SECONDS`), and stops at
`MAX_INDEXED_SITES` (50,000) or when the queue empties. Measured on a cold local
database with no manual command: boot in **608 ms**, `/health` in **11 ms** while
crawling, and after four minutes **113 sites / 269 pages** (113 root + 156
sitelink) with **480 domains discovered** and the queue still draining.

A pass crawls each seed's entry page and follows same-host **level-1 links only**,
storing one root page plus at most five sitelinks per domain with
`INSERT OR REPLACE` so a re-crawl refreshes rather than duplicates.

**Nothing here needs a human.** On boot the lifespan checks coverage and, when
the index is under `SEED_COVERAGE_MIN_SITES` (5), `SEED_COVERAGE_MIN_RATIO` (1.0), `DISCOVERY_BATCH_SIZE` (10),
`DISCOVERY_BATCH_PAUSE_SECONDS` (0.5), `DISCOVERY_INTERVAL_SECONDS` (300), `MAX_INDEXED_SITES` (50000), launches
`run_autonomous_seed_crawl()` with `asyncio.create_task`, crawling **only the
missing seeds** so repeated boots make progress instead of repeating work. The
port binds and `/health` answers in milliseconds while the crawl runs behind it. The same
function wakes every `SEED_CRAWL_INTERVAL_SECONDS` (24 h) to re-verify the seeds
and pick up new authoritative links, and it never raises out of the loop: a dead
domain is logged and skipped, and the pass carries on.

Measured on a cold local database with no manual command: lifespan returned in
**300 ms**, `/health` answered **200 in 12 ms** while the crawl was in flight,
and by **+20 s** the index held 5 sites / 26 pages (5 root + 21 sitelink), all
26 in `page_fts` — `bank` → RBI in 15 ms, `karnataka` → bengaluruurban.nic.in in
4 ms, `india` → 3 hits.

To run it by hand (optional, not required):

```bash
python -m app.indexer.crawler --list
python -m app.indexer.crawler                      # the five India-first categories
python -m app.indexer.crawler --category banking
python -m app.indexer.crawler --verify             # sites, pages, sitelinks, sample searches
```

**Depth and budget rules.** Root and level-1 pages only
(`INDEX_MAX_PATH_DEPTH=1`), with main-namespace Wikipedia articles as the
documented exception — `/wiki/<Article>` *is* their entry-point shape. Each
document stores the clean canonical title, the normalised URL, the domain, the
category and a **3–4 sentence factual summary** capped at 1.5 KB
(`INDEX_SUMMARY_SENTENCES`, `INDEX_CONTENT_MAX_CHARS`), inside a row budget of
1.5 KB (`INDEX_MAX_PAYLOAD_BYTES`), sitelinks included. Scripts, styles,
navigation, cookie notices, footers, ads and inline images never reach storage.

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

**Routing** (`app/services/search_service.py`) — live-fetch-once, cache-forever:

```
documents_fts  →  any hit          →  source: "index"   (no outbound call, ever)
              →  Turso result cache →  source: "cache"   (no outbound call)
              →  zero local hits    →  live fetch once   →  source: "live"
                                        ↘ ingest the live results into documents_fts
                                        ↘ (or queue the query if none were indexable)
```

The first query for a topic pays for one live fetch and its results are written
straight into the index **in the same request**, with an explicit log line:

```
Auto-ingested 3 documents for query: Github into Turso ({'duplicate': 0, ...})
```

 — title, URL and snippet, fitted to the same 2 KB budget,
through the same dedupe and domain caps. Every later query for that topic is a
`documents_fts` hit: no provider call, single-digit milliseconds.

Verified through the API with `q=Github`:

| request | source | took_ms | live calls |
| --- | --- | --- | --- |
| 1 | `eyesearch_live` | 9 | 1 (auto-ingested 1 document) |
| 2 | `eyesearch_index` | 15 | still 1 |
| 3 | `eyesearch_index` | 1 | still 1 |

`auto_ingested` on the response reports how many documents that request wrote.
A query whose live results are all rejected by the crawl rules (repository
loops, deep paths) is queued for the crawler instead, and a repeat of it within
ten minutes is answered locally rather than retried against the providers.
`LIVE_SEARCH_ENABLED=false` gives a strictly local deployment where a miss
returns `source: "queued"` and nothing outbound happens at all.

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
`INDEX_MAX_PATH_DEPTH` (2), `LIVE_SEARCH_ENABLED` (true),
`RELEVANCE_MIN_TOKEN_COVERAGE` (0.6), `ON_DEMAND_MAX_CANDIDATES` (8),
`ON_DEMAND_MAX_FETCHES` (5), `ON_DEMAND_CONCURRENCY` (3),
`ON_DEMAND_BUDGET_SECONDS` (12), `INDEX_SUMMARY_SENTENCES` (4),
`INDEX_MAX_SITE_PAYLOAD_BYTES` (10240),
`SEED_CRAWL_ENABLED` (true), `SEED_CRAWL_INTERVAL_SECONDS` (86400),
`SEED_CRAWL_PAGES_PER_DOMAIN` (5), `SEED_CRAWL_CONCURRENCY` (5), `SEED_COVERAGE_MIN_SITES` (5), `SEED_COVERAGE_MIN_RATIO` (1.0), `DISCOVERY_BATCH_SIZE` (10),
`DISCOVERY_BATCH_PAUSE_SECONDS` (0.5), `DISCOVERY_INTERVAL_SECONDS` (300), `MAX_INDEXED_SITES` (50000),
`CRAWL_TIMEOUT_SECONDS` (15), `CRAWL_VERIFY_TLS` (auto), `INDEX_SECTIONS_MAX` (5),
`PUBLIC_BASE_URL` ("")
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
