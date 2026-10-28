# Eyesearch indexing architecture

Eyesearch uses a focused, quality-first index rather than attempting to mirror Google's private infrastructure. It applies established search-engine principles: canonical URLs, crawl policy, duplicate filtering, page-level titles and descriptions, full-text retrieval, ranking, and result grouping.

## Per-site payload budget

Each domain has a configurable budget for the UTF-8 bytes of fields stored in the `pages` table across all indexed pages on that domain. The default is 10 KiB (10,240 bytes).

Set `MAX_SITE_PAYLOAD_BYTES` in the deployment environment to change the budget later; for example, `20480` sets a 20 KiB budget. No code change is needed to raise or lower the configured limit.

The current budget counts URL, title, description, body text, digest, category, and language. It does not count FTS5 index pages, SQLite/Turso row and index overhead, replication, or provider-level storage overhead. Therefore it is a content-payload budget, not a hard 10 KiB guarantee for total database storage.

## Page selection and search quality

- The budget applies across all stored pages for a domain, not independently to every page.
- Every indexed page retains its own URL, title, description, and body text.
- Query-time matching uses FTS5 and BM25; snippets should come from the matching passage.
- Duplicate filtering and crawl quality/policy checks remain separate from the size budget.
- Once a site's budget is reached, additional discovered URLs for that site are not scheduled for further indexing by this path.

## Operational notes

- The setting affects new indexing decisions. It does not automatically shrink or rewrite pages already stored when the budget is lowered.
- Raising the setting permits future pages to be indexed; previously skipped URLs need to be re-queued by the crawler's normal frontier/rearm behavior or an explicit re-crawl.
- Validate storage using the actual Turso database size, because FTS5 and SQLite overhead depend on content and indexes.
- This is not Google's exact architecture: Google's internal crawling and ranking systems are proprietary and operate at a vastly different scale. Eyesearch implements a compact, explainable architecture suited to its own constraints.
