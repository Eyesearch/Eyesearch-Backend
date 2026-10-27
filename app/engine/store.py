"""Storage for the two-tier index: `sites`, `pages`, `page_fts`.

One `sites` row per domain, a bounded set of `pages` rows per site (the root plus
its sitelinks), and an external-content FTS5 index over the pages' term fields.
The triggers own the FTS writes, so this module never touches `page_fts` except
to read it.

The public API is unchanged from the flat model: callers hand over extracted
documents and get ranked site cards back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from app.database import execute_safe, get_client
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.ranking import RankedDocument, rank_documents
from app.engine.relevance import is_relevant
from app.engine.schema import BM25_WEIGHTS
from app.engine.text import detect_language, rich_snippet, truncate
from app.engine.urls import domain_class, is_indexable, normalise_url, root_domain
from app.errors import DatabaseError, StorageQuotaError

logger = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9\u0900-\u097F\u0C80-\u0CFF]+")

_BM25_LIST = ", ".join(f"{weight:.1f}" for weight in BM25_WEIGHTS)

# The retrieval query from the brief: weighted bm25, live pages only, best first.
_SEARCH_SQL = f"""
SELECT p.page_id, p.site_id, s.domain, s.name, s.category, p.url, p.title,
       p.description, p.content, p.kind, p.last_crawled_at,
       bm25(page_fts, {_BM25_LIST}) AS lexical_score
FROM page_fts
JOIN pages p ON p.page_id = page_fts.rowid
JOIN sites s ON s.site_id = p.site_id
WHERE page_fts MATCH ? AND p.http_status = 200 AND p.noindex = 0
ORDER BY lexical_score ASC
LIMIT ?
"""

_SUGGEST_SQL = f"""
SELECT p.title
FROM page_fts
JOIN pages p ON p.page_id = page_fts.rowid
WHERE page_fts MATCH ? AND p.http_status = 200 AND p.noindex = 0
ORDER BY bm25(page_fts, {_BM25_LIST}) ASC
LIMIT ?
"""

_SITE_UPSERT = """
INSERT INTO sites (domain, canonical_url, name, category, authority_score, last_crawled_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(domain) DO UPDATE SET
    canonical_url = excluded.canonical_url,
    name = excluded.name,
    category = excluded.category,
    authority_score = excluded.authority_score,
    last_crawled_at = excluded.last_crawled_at
"""

_PAGE_UPSERT = """
INSERT INTO pages (
    site_id, url, kind, title, headings, description, content, nav_terms, site_terms,
    category_terms, url_terms, http_status, noindex, canonical_url, content_hash, last_crawled_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET
    site_id = excluded.site_id,
    kind = excluded.kind,
    title = excluded.title,
    headings = excluded.headings,
    description = excluded.description,
    content = excluded.content,
    nav_terms = excluded.nav_terms,
    site_terms = excluded.site_terms,
    category_terms = excluded.category_terms,
    url_terms = excluded.url_terms,
    http_status = excluded.http_status,
    noindex = excluded.noindex,
    canonical_url = excluded.canonical_url,
    content_hash = excluded.content_hash,
    last_crawled_at = excluded.last_crawled_at
"""

_PAGE_REPLACE = """
INSERT OR REPLACE INTO pages (
    site_id, url, kind, title, headings, description, content, nav_terms, site_terms,
    category_terms, url_terms, http_status, noindex, canonical_url, content_hash, last_crawled_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


async def _batch(client: Any, statements: Sequence[tuple[str, list[Any]]], *, strict: bool = True) -> None:
    """Run a batch, using the non-strict mode where the driver supports it.

    The remote pipeline client can report which steps failed without discarding
    the ones that committed; the local sqlite client has no such option.
    """
    try:
        await client.batch(list(statements), strict=strict)
    except TypeError:
        await client.batch(list(statements))
    except Exception as exc:
        # Imported here: the guard reaches back into this module for counts.
        from app.indexer.guard import is_quota_error, trip_quota

        if is_quota_error(exc):
            trip_quota(str(exc)[:200])
            raise StorageQuotaError(f"database storage exhausted: {exc}") from exc
        raise


# A domain in one of these states may be re-armed once its cooldown expires.
REARMABLE_STATUSES: frozenset[str] = frozenset({"processed", "failed"})


def _older_than(stamp: Any, cooldown_seconds: int) -> bool:
    """True when `stamp` (a SQLite timestamp) is older than the cooldown.

    Timestamps are stored as `YYYY-MM-DD HH:MM:SS` in UTC, which compares
    lexicographically, so no driver-specific date handling is needed.
    """
    if not stamp:
        return True
    try:
        moment = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - moment).total_seconds() >= max(0, int(cooldown_seconds))


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _content_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression."""
    tokens = _SAFE_TOKEN.findall(query or "")
    if not tokens:
        return ""
    quoted = [f'"{token}"' for token in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)


def query_tokens(query: str) -> list[str]:
    return [token.lower() for token in _SAFE_TOKEN.findall(query or "") if len(token) >= 2]


def passes_relevance(query: str, document: RankedDocument, settings: EngineSettings) -> bool:
    """Reject a hit that only matched a passing mention (see engine/relevance)."""
    section_text = " ".join(
        f"{section.get('title', '')} {section.get('url', '')}"
        for section in (document.sections or [])
    )
    # The extracted body is now part of the record (up to 1.2 KB of clean prose),
    # so a term that appears only there is a genuine match rather than a footer
    # artefact — navigation and footers are stripped before this point.
    body = truncate(str(getattr(document, "body", "") or ""), 1200)
    return is_relevant(
        query,
        title=document.title or "",
        url=document.url or "",
        snippet=f"{document.snippet or ''} {section_text} {body}".strip(),
        min_coverage=settings.relevance_min_token_coverage,
    )


def _parse_sections(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {
            "title": str(item.get("title"))[:80],
            "url": str(item.get("url"))[:300],
            "snippet": str(item.get("snippet") or "")[:120],
        }
        for item in parsed
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]


def _authority(url: str) -> float:
    return {"government": 1.0, "news": 0.8, "knowledge": 0.9}.get(domain_class(url), 0.6)


class IndexStore:
    """Typed access to the two-tier index."""

    def __init__(self, settings: EngineSettings | None = None) -> None:
        self.settings = settings or get_engine_settings()
        self._backlog_kind: bool | None = None

    async def ensure_schema(self) -> None:
        from app import database

        await database.initialize_schema()

    # -- ingestion ------------------------------------------------------
    async def ingest_documents(
        self,
        documents: Sequence[dict[str, Any]],
        *,
        mode: str = "insert",
    ) -> dict[str, int]:
        """Write sites and pages. `mode="upsert"` refreshes, `"insert"` adds."""
        return await self._write(documents, mode=mode, replace=False)

    async def replace_documents(self, documents: Sequence[dict[str, Any]]) -> dict[str, int]:
        """`INSERT OR REPLACE` per page — the crawler's path."""
        return await self._write(documents, mode="replace", replace=True)

    async def _write(
        self,
        documents: Sequence[dict[str, Any]],
        *,
        mode: str,
        replace: bool,
    ) -> dict[str, int]:
        outcome = {
            "inserted": 0, "replaced": 0, "duplicate": 0, "domain_cap": 0,
            "not_indexable": 0, "over_budget": 0, "collapsed": 0,
            "sites": 0, "pages": 0, "sitelinks": 0,
        }
        if not documents:
            return outcome

        client = get_client()
        stamp = _now_epoch()
        upsert = mode in {"upsert", "replace"}

        # --- 1. normalise and validate --------------------------------
        prepared: list[dict[str, Any]] = []
        for doc in documents:
            url = normalise_url(str(doc.get("url") or ""))
            title = str(doc.get("title") or "").strip()
            if not url or not title:
                outcome["not_indexable"] += 1
                continue
            if not is_indexable(url):
                outcome["not_indexable"] += 1
                continue
            payload = int(doc.get("payload_bytes") or 0)
            if payload > self.settings.max_payload_bytes:
                outcome["over_budget"] += 1
                continue
            prepared.append({"doc": doc, "url": url, "title": title, "payload": payload})

        if not prepared:
            return outcome

        # --- 2. ensure a site row per host ----------------------------
        hosts: dict[str, dict[str, Any]] = {}
        for item in prepared:
            host = urlparse(item["url"]).netloc
            if not host:
                outcome["not_indexable"] += 1
                continue
            hosts.setdefault(host, item)

        site_statements: list[tuple[str, list[Any]]] = []
        for host, item in hosts.items():
            site_statements.append(
                (
                    _SITE_UPSERT,
                    [
                        host,
                        f"https://{host}/",
                        str(item["doc"].get("site_name") or item["title"])[:200],
                        str(item["doc"].get("category") or domain_class(item["url"]))[:40],
                        _authority(item["url"]),
                        stamp,
                    ],
                )
            )
        try:
            # Sites commit on their own, in their own request: a failure writing
            # pages later can never take the site rows with it.
            await _batch(client, site_statements, strict=False)
        except StorageQuotaError:
            raise
        except Exception as exc:
            logger.error(
                "site write failed for %s: %s: %s (statement: %s)",
                ", ".join(hosts)[:200], type(exc).__name__, exc,
                _SITE_UPSERT.strip().splitlines()[0][:80],
            )
            raise DatabaseError(f"site write failed: {exc}") from exc
        logger.debug("site rows written: %d", len(site_statements))

        site_ids: dict[str, int] = {}
        for host in hosts:
            try:
                row = await execute_safe("SELECT site_id FROM sites WHERE domain = ? LIMIT 1", [host])
                rows = getattr(row, "rows", ()) or ()
                if rows:
                    site_ids[host] = int(rows[0][0])
            except Exception as exc:
                logger.warning("site lookup failed for %s: %s", host, exc)
        outcome["sites"] = len(site_ids)

        # --- 3. pages: the root plus its bounded sitelinks ------------
        statements: list[tuple[str, list[Any]]] = []
        site_payload: dict[str, int] = {}
        seen_pages: set[str] = set()
        root_seen: set[str] = set()  # one root page per domain, never two
        sitelinks_per_host: dict[str, int] = {}  # bounded at sections_max per domain
        page_statement = _PAGE_REPLACE if replace else _PAGE_UPSERT

        for item in prepared:
            doc, url, title, payload = item["doc"], item["url"], item["title"], item["payload"]
            host = urlparse(url).netloc
            site_id = site_ids.get(host)
            if not site_id:
                outcome["not_indexable"] += 1
                continue

            site_payload.setdefault(host, 0)
            if site_payload[host] + payload > self.settings.max_site_payload_bytes:
                outcome["over_budget"] += 1
                continue
            site_payload[host] += payload

            existed = False
            try:
                probe = await execute_safe("SELECT 1 FROM pages WHERE url = ? LIMIT 1", [url])
                existed = bool(getattr(probe, "rows", ()))
            except Exception as exc:
                logger.debug("page probe failed for %s: %s", url, exc)

            if existed and not upsert:
                # The crawler's rule: a URL is indexed once, then revalidated.
                outcome["duplicate"] += 1
                continue

            is_root_path = urlparse(url).path in ("", "/")
            if not is_root_path and sitelinks_per_host.get(host, 0) >= self.settings.sections_max:
                # A domain carries at most `sections_max` sitelinks, whether they
                # came from a level-1 page or from the root's navigation.
                outcome["domain_cap"] += 1
                continue

            is_root = urlparse(url).path in ("", "/")
            if is_root:
                # Exactly one root per site: a language variant of "/" is the same
                # entry point, not a second root.
                if host in root_seen:
                    outcome["duplicate"] += 1
                    continue
                try:
                    existing_root = await execute_safe(
                        "SELECT 1 FROM pages WHERE site_id = ? AND kind = 'root' LIMIT 1",
                        [site_id],
                    )
                    if getattr(existing_root, "rows", ()) and not upsert:
                        outcome["duplicate"] += 1
                        continue
                except Exception as exc:
                    logger.debug("root probe failed for %s: %s", host, exc)
                root_seen.add(host)
            statements.append(
                (
                    page_statement,
                    [
                        site_id,
                        url,
                        "root" if is_root else "sitelink",
                        title,
                        str(doc.get("headings") or "")[:500],
                        str(doc.get("description") or doc.get("snippet") or "")[:500],
                        str(doc.get("content") or doc.get("body_text") or "")[:1200],
                        str(doc.get("nav_terms") or "")[:500],
                        str(doc.get("site_terms") or host.replace(".", " "))[:300],
                        str(doc.get("category_terms") or doc.get("category") or "")[:80],
                        str(doc.get("url_terms") or "")[:300],
                        200,
                        0,
                        doc.get("canonical_url") or url,
                        _content_hash(title, str(doc.get("description") or "")),
                        stamp,
                    ],
                )
            )
            seen_pages.add(url)
            if not is_root_path:
                sitelinks_per_host[host] = sitelinks_per_host.get(host, 0) + 1
            outcome["pages"] += 1
            outcome["replaced" if existed else "inserted"] += 1

            for section in (doc.get("sections") or [])[: self.settings.sections_max]:
                if sitelinks_per_host.get(host, 0) >= self.settings.sections_max:
                    break  # the domain already has its five
                section_url = normalise_url(str(section.get("url") or ""))
                section_title = str(section.get("title") or "").strip()
                if not section_url or not section_title or section_url in seen_pages:
                    continue
                if urlparse(section_url).netloc != host:
                    continue
                statements.append(
                    (
                        page_statement,
                        [
                            site_id,
                            section_url,
                            "sitelink",
                            section_title[:120],
                            "",
                            str(section.get("snippet") or "")[:200],
                            str(section.get("snippet") or section_title)[:300],
                            section_title[:200],
                            str(doc.get("site_terms") or host.replace(".", " "))[:300],
                            str(doc.get("category_terms") or doc.get("category") or "")[:80],
                            " ".join(
                                part for part in urlparse(section_url).path.split("/") if part
                            )[:200],
                            200,
                            0,
                            section_url,
                            _content_hash(section_title, str(section.get("snippet") or "")),
                            stamp,
                        ],
                    )
                )
                seen_pages.add(section_url)
                sitelinks_per_host[host] = sitelinks_per_host.get(host, 0) + 1
                outcome["pages"] += 1
                outcome["sitelinks"] += 1

        if statements:
            logger.debug(
                "writing %d pages for %d sites (%d sitelinks)",
                outcome["pages"], len(site_ids), outcome["sitelinks"],
            )
            try:
                await _batch(client, statements, strict=False)
                errors = list(getattr(client, "last_errors", []) or [])
                if errors:
                    outcome["failed_statements"] = len(errors)
                    for message in errors[:3]:
                        logger.error("page write step failed: %s", message)
            except StorageQuotaError:
                raise
            except Exception as exc:
                logger.error(
                    "page write failed for %s: %s: %s (statement: %s)",
                    ", ".join(sorted(site_payload))[:200], type(exc).__name__, exc,
                    _PAGE_UPSERT.strip().splitlines()[0][:80],
                )
                raise DatabaseError(f"index write failed: {exc}") from exc

        if outcome["pages"]:
            logger.info(
                "indexed %d pages across %d sites (%d sitelinks, %d rows rejected)",
                outcome["pages"],
                len(site_ids),
                outcome["sitelinks"],
                outcome["not_indexable"] + outcome["over_budget"] + outcome["duplicate"]
                + outcome["domain_cap"],
            )
        elif prepared:
            logger.warning(
                "nothing stored from %d documents: not_indexable=%d over_budget=%d duplicate=%d domain_cap=%d",
                len(prepared), outcome["not_indexable"], outcome["over_budget"],
                outcome["duplicate"], outcome["domain_cap"],
            )
        return outcome

    # -- reading --------------------------------------------------------
    async def search_local(self, query: str, limit: int | None = None) -> list[RankedDocument]:
        """Weighted bm25 over `page_fts`, grouped into one card per site."""
        match = build_match_query(query)
        if not match:
            return []

        capped = max(limit or self.settings.index_limit, 1)
        try:
            result = await execute_safe(_SEARCH_SQL, [match, min(capped * 5, 50)])
        except Exception as exc:
            logger.warning("index query failed for %r: %s", query, exc)
            return []

        columns = tuple(getattr(result, "columns", ()) or ())
        rows = [dict(zip(columns, row)) for row in (getattr(result, "rows", ()) or ())]
        if not rows:
            return []

        grouped = await self._group_by_site(rows)
        relevant = [doc for doc in grouped if passes_relevance(query, doc, self.settings)]
        if len(relevant) != len(grouped):
            logger.debug(
                "index: %d of %d sites for %r rejected as weak matches",
                len(grouped) - len(relevant), len(grouped), query,
            )
        return rank_documents(relevant, freshness_weight=self.settings.freshness_weight)[:capped]

    async def _group_by_site(self, rows: list[dict[str, Any]]) -> list[RankedDocument]:
        """Collapse matched pages into one document per site, with its sitelinks."""
        client = get_client()
        best: dict[int, dict[str, Any]] = {}
        for row in rows:
            site_id = int(row.get("site_id") or 0)
            if not site_id:
                continue
            score = float(row.get("lexical_score") or 0.0)
            current = best.get(site_id)
            if current is None or score < float(current.get("lexical_score") or 0.0):
                best[site_id] = row

        documents: list[RankedDocument] = []
        for site_id, matched in best.items():
            try:
                pages = await execute_safe(
                    "SELECT url, kind, title, description, content, last_crawled_at FROM pages "
                    "WHERE site_id = ? AND http_status = 200 AND noindex = 0 "
                    "ORDER BY CASE kind WHEN 'root' THEN 0 ELSE 1 END, page_id LIMIT 12",
                    [site_id],
                )
                page_rows = getattr(pages, "rows", ()) or ()
            except Exception as exc:
                logger.warning("page fetch failed for site %s: %s", site_id, exc)
                page_rows = ()

            root = None
            sitelinks: list[dict[str, str]] = []
            bodies: dict[str, str] = {}
            for url, kind, title, description, content, crawled in page_rows:
                bodies[str(url)] = str(content or "")
                if kind == "root" and root is None:
                    root = {
                        "url": url,
                        "title": title,
                        "description": description,
                        "content": content,
                        "crawled": crawled,
                    }
                elif kind == "sitelink":
                    sitelinks.append(
                        {"title": str(title), "url": str(url), "snippet": str(description or "")}
                    )

            domain = str(matched.get("domain") or root_domain(str(matched.get("url") or "")))
            primary = root or {
                "url": matched.get("url"),
                "title": matched.get("title"),
                "description": matched.get("description"),
                "content": matched.get("content"),
                "crawled": matched.get("last_crawled_at"),
            }
            # A site whose only matched page is a sitelink still has body text:
            # take it from that page rather than losing the match.
            if not primary.get("content"):
                primary["content"] = bodies.get(str(primary.get("url") or ""), "")
            title = str(primary["title"] or "")
            body = str(primary.get("content") or "")
            documents.append(
                RankedDocument(
                    url=str(primary["url"]),
                    title=title,
                    # never repeat the domain back at the reader: a boilerplate
                    # description is replaced by the opening of the body text
                    snippet=rich_snippet(
                        str(primary["description"] or ""), body, title,
                        limit=self.settings.snippet_max_chars,
                    ),
                    language=detect_language(f"{primary['title']} {primary['description'] or ''}"),
                    updated_at=str(primary["crawled"] or "") or None,
                    bm25=float(matched.get("lexical_score") or 0.0),
                    # The schema stores no icon; the conventional location is a
                    # fine fallback and the UI degrades to a letter tile anyway.
                    favicon_url=f"https://{domain}/favicon.ico" if domain else None,
                    body=str(primary.get("content") or ""),
                    sections=sitelinks[: self.settings.sections_max],
                )
            )
        return documents

    async def suggest(self, query: str, limit: int = 8) -> list[str]:
        tokens = _SAFE_TOKEN.findall(query or "")
        if not tokens:
            return []
        client = get_client()
        seen: set[str] = set()
        suggestions: list[str] = []
        for match in (build_match_query(query), f'"{tokens[-1]}"*'):
            if not match:
                continue
            try:
                result = await execute_safe(_SUGGEST_SQL, [match, max(limit * 2, 12)])
            except Exception as exc:
                logger.warning("suggest query failed for %r: %s", query, exc)
                continue
            for row in getattr(result, "rows", ()) or ():
                title = str(row[0] or "").strip()
                if not title or title.lower() in seen:
                    continue
                seen.add(title.lower())
                suggestions.append(title[:80])
                if len(suggestions) >= limit:
                    return suggestions
        return suggestions

    async def get_document(self, url: str) -> dict[str, Any] | None:
        """A page in the shape callers used before the two-tier split."""
        client = get_client()
        key = normalise_url(url)
        try:
            result = await execute_safe(
                "SELECT page_id, site_id, url, kind, title, description, content, canonical_url, "
                "content_hash, last_crawled_at FROM pages WHERE url = ? LIMIT 1",
                [key],
            )
        except Exception as exc:
            raise DatabaseError(f"page read failed: {exc}") from exc
        rows = getattr(result, "rows", ()) or ()
        if not rows:
            return None

        (
            page_id, site_id, page_url, kind, title, description, content,
            canonical, content_hash, crawled,
        ) = rows[0]
        site = None
        try:
            site_row = await execute_safe(
                "SELECT domain, name, category FROM sites WHERE site_id = ? LIMIT 1", [int(site_id)]
            )
            site_rows = getattr(site_row, "rows", ()) or ()
            site = site_rows[0] if site_rows else None
        except Exception as exc:
            logger.debug("site read failed for %s: %s", site_id, exc)

        return {
            "url": str(page_url),
            "kind": str(kind),
            "title": str(title),
            "snippet": str(description or ""),
            "description": str(description or ""),
            "content": str(content or ""),
            "canonical_url": str(canonical or page_url),
            "content_hash": str(content_hash or ""),
            "domain": str(site[0]) if site else root_domain(page_url),
            "category": str(site[2]) if site else "",
            "site_name": str(site[1]) if site else "",
            "last_crawled_at": crawled,
            "page_id": int(page_id),
            "site_id": int(site_id),
        }

    async def count_documents(self) -> int:
        """Sites in the index (one card per site)."""
        client = get_client()
        result = await execute_safe("SELECT COUNT(*) FROM sites")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def count_pages(self) -> int:
        client = get_client()
        result = await execute_safe("SELECT COUNT(*) FROM pages")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def max_payload_bytes(self) -> int:
        client = get_client()
        result = await execute_safe(
            "SELECT COALESCE(MAX(LENGTH(title) + LENGTH(headings) + LENGTH(description) "
            "+ LENGTH(nav_terms) + LENGTH(site_terms) + LENGTH(category_terms) + LENGTH(url_terms)), 0) "
            "FROM pages"
        )
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def domain_counts(self) -> dict[str, int]:
        client = get_client()
        result = await execute_safe("SELECT domain, COUNT(*) FROM sites GROUP BY domain")
        return {str(row[0] or ""): int(row[1]) for row in getattr(result, "rows", ()) or ()}

    async def stats(self) -> dict[str, int]:
        try:
            sites = await self.count_documents()
            pages = await self.count_pages()
            client = get_client()
            backlog = await execute_safe("SELECT COUNT(*) FROM crawl_backlog WHERE status = 'pending'")
            rows = getattr(backlog, "rows", ()) or ()
            pending = int(rows[0][0]) if rows else 0
            return {
                "documents": sites,
                "sites": sites,
                "pages": pages,
                "backlog": pending,
                "max_payload_bytes": await self.max_payload_bytes(),
            }
        except Exception as exc:
            logger.warning("index stats unavailable: %s", exc)
            return {"documents": 0, "sites": 0, "pages": 0, "backlog": 0, "max_payload_bytes": 0}

    # -- revalidation ---------------------------------------------------
    async def stale_documents(self, limit: int = 10) -> list[dict[str, Any]]:
        client = get_client()
        result = await execute_safe(
            "SELECT p.url, s.domain, s.category, p.last_crawled_at FROM pages p "
            "JOIN sites s ON s.site_id = p.site_id "
            "ORDER BY COALESCE(p.last_crawled_at, 0) ASC LIMIT ?",
            [max(limit * 5, 25)],
        )
        columns = tuple(getattr(result, "columns", ()) or ())
        rows = [dict(zip(columns, row)) for row in (getattr(result, "rows", ()) or ())]

        now = datetime.now(timezone.utc)
        ttls = {
            "news": timedelta(days=self.settings.news_ttl_days),
            "government": timedelta(days=self.settings.government_ttl_days),
            "static": timedelta(days=self.settings.static_ttl_days),
        }
        stale: list[dict[str, Any]] = []
        for record in rows:
            crawled = record.get("last_crawled_at")
            ttl = ttls.get(str(record.get("category") or "static"), ttls["static"])
            if not crawled:
                stale.append(record)
            else:
                when = datetime.fromtimestamp(int(crawled), tz=timezone.utc)
                if now - when >= ttl:
                    stale.append(record)
            if len(stale) >= limit:
                break
        return stale

    async def update_document(
        self,
        url: str,
        document: dict[str, Any],
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        """Refresh one page in place (revalidation)."""
        key = normalise_url(url)
        client = get_client()
        probe = await execute_safe("SELECT page_id FROM pages WHERE url = ? LIMIT 1", [key])
        rows = getattr(probe, "rows", ()) or ()
        if not rows:
            return False

        try:
            await execute_safe(
                "UPDATE pages SET title = ?, description = ?, headings = ?, nav_terms = ?, "
                "content_hash = ?, last_crawled_at = ? WHERE url = ?",
                [
                    str(document.get("title") or "")[:300],
                    str(document.get("description") or document.get("snippet") or "")[:500],
                    str(document.get("headings") or "")[:500],
                    str(document.get("nav_terms") or "")[:500],
                    _content_hash(str(document.get("title") or ""), str(document.get("description") or "")),
                    _now_epoch(),
                    key,
                ],
            )
        except Exception as exc:
            raise DatabaseError(f"page update failed: {exc}") from exc
        return True

    # -- crawler backlog ------------------------------------------------
    async def enqueue_backlog(self, query: str, hits: int) -> None:
        cleaned = (query or "").strip().lower()
        if not cleaned:
            return
        client = get_client()
        try:
            if await self._has_backlog_kind():
                await execute_safe(
                    """
                    INSERT INTO crawl_backlog (query, kind, hits, status, created_at)
                    VALUES (?, 'query', ?, 'pending', CURRENT_TIMESTAMP)
                    ON CONFLICT(query) DO UPDATE SET hits = crawl_backlog.hits + 1, status = 'pending'
                    """,
                    [cleaned, max(0, hits)],
                )
            else:
                # A database that predates the `kind` column still takes queries.
                await execute_safe(
                    """
                    INSERT INTO crawl_backlog (query, hits, status, created_at)
                    VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)
                    ON CONFLICT(query) DO UPDATE SET hits = crawl_backlog.hits + 1, status = 'pending'
                    """,
                    [cleaned, max(0, hits)],
                )
        except Exception as exc:
            logger.warning("backlog enqueue failed for %r: %s", query, exc)

    async def recently_queued(self, query: str, within_minutes: int = 10) -> bool:
        cleaned = (query or "").strip().lower()
        if not cleaned:
            return False
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=max(1, within_minutes))
        ).strftime("%Y-%m-%d %H:%M:%S")
        client = get_client()
        try:
            clause = await self._kind_clause("query")
            result = await execute_safe(
                f"SELECT 1 FROM crawl_backlog WHERE query = ?{clause} "
                "AND status = 'pending' AND created_at >= ? LIMIT 1",
                [cleaned, cutoff],
            )
            return bool(getattr(result, "rows", ()))
        except Exception as exc:
            logger.debug("backlog probe failed for %r: %s", query, exc)
            return False

    async def url_is_known(self, url: str) -> bool:
        """True when a URL is already indexed or already queued."""
        try:
            page = await execute_safe("SELECT 1 FROM pages WHERE url = ? LIMIT 1", [url])
            if getattr(page, "rows", ()):
                return True
            queued = await execute_safe("SELECT 1 FROM crawl_queue WHERE url = ? LIMIT 1", [url])
            return bool(getattr(queued, "rows", ()))
        except Exception as exc:
            logger.debug("url_is_known failed for %s: %s", url, exc)
            return False

    async def queue_url(self, url: str, *, domain: str = "", reason: str = "discovered") -> None:
        """Mirror a URL into `crawl_queue` so the buffer is visible."""
        await execute_safe(
            "INSERT INTO crawl_queue (url, domain, category, depth, reason, status) "
            "VALUES (?, ?, NULL, 0, ?, 'pending') "
            "ON CONFLICT(url) DO UPDATE SET status = 'pending', reason = excluded.reason",
            [url, domain or None, reason],
        )

    async def close_queue_url(self, url: str, *, status: str = "processed") -> None:
        await execute_safe(
            "UPDATE crawl_queue SET status = ? WHERE url = ?", [status, url]
        )

    async def pending_queue_hosts(self, limit: int = 10) -> list[str]:
        """Distinct hosts with pending URLs in `crawl_queue`."""
        result = await execute_safe(
            "SELECT DISTINCT domain FROM crawl_queue WHERE status = 'pending' "
            "AND domain IS NOT NULL ORDER BY domain ASC LIMIT ?",
            [max(1, int(limit))],
        )
        rows = getattr(result, "rows", ()) or ()
        return [str(row[0]) for row in rows if row and row[0]]

    async def queue_depth(self) -> int:
        result = await execute_safe(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        )
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def purge_blocked_queries(self) -> int:
        """Move queries whose only outcome was a block straight to history."""
        where = (
            "COALESCE(kind, 'query') = 'query' AND status = 'pending' "
            "AND (last_error LIKE '%403%' OR last_error LIKE '%202%' OR last_error LIKE '%blocked%')"
        )
        counted = await execute_safe(f"SELECT COUNT(*) FROM crawl_backlog WHERE {where}")
        rows = getattr(counted, "rows", ()) or ()
        count = int(rows[0][0]) if rows else 0
        if not count:
            return 0
        await execute_safe(
            "UPDATE crawl_backlog SET status = 'dropped', processed_at = CURRENT_TIMESTAMP "
            f"WHERE {where}"
        )
        logger.info("purged %d blocked backlog queries to history", count)
        return count

    async def note_backlog_attempt(self, query: str, *, reason: str = "") -> int:
        """Record one failed resolution and return the running attempt count."""
        await execute_safe(
            "UPDATE crawl_backlog SET attempts = COALESCE(attempts, 0) + 1, "
            "last_error = ? WHERE query = ? AND COALESCE(kind, 'query') = 'query'",
            [reason[:200], query],
        )
        result = await execute_safe(
            "SELECT COALESCE(attempts, 0) FROM crawl_backlog WHERE query = ? LIMIT 1", [query]
        )
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def mark_backlog_dropped(self, query: str, *, reason: str = "") -> None:
        """Move a query out of the queue and into history."""
        await execute_safe(
            "UPDATE crawl_backlog SET status = 'dropped', processed_at = CURRENT_TIMESTAMP, "
            "last_error = ? WHERE query = ? AND COALESCE(kind, 'query') = 'query'",
            [reason[:200] or "dropped", query],
        )

    async def pending_backlog(self, limit: int = 20) -> list[str]:
        client = get_client()
        clause = await self._kind_clause("query")
        result = await execute_safe(
            f"SELECT query FROM crawl_backlog WHERE status = 'pending'{clause} "
            "ORDER BY hits DESC, created_at ASC LIMIT ?",
            [limit],
        )
        return [str(row[0]) for row in getattr(result, "rows", ()) or ()]

    async def mark_backlog_processed(self, query: str) -> None:
        client = get_client()
        clause = await self._kind_clause("query")
        await execute_safe(
            "UPDATE crawl_backlog SET status = 'processed', processed_at = CURRENT_TIMESTAMP "
            f"WHERE query = ?{clause}",
            [query.strip().lower()],
        )

    async def _has_backlog_kind(self) -> bool:
        """Whether `crawl_backlog` carries the `kind` column (cached per process)."""
        if self._backlog_kind is not None:
            return self._backlog_kind
        try:
            result = await execute_safe("PRAGMA table_info(crawl_backlog)")
            columns = {str(row[1]).lower() for row in getattr(result, "rows", ()) or ()}
            self._backlog_kind = "kind" in columns
            if not self._backlog_kind:
                logger.error(
                    "SCHEMA MISMATCH: crawl_backlog has no `kind` column — falling back to "
                    "the legacy insert. Run the app once so the migration can apply."
                )
        except Exception as exc:
            logger.warning("could not inspect crawl_backlog: %s", exc)
            self._backlog_kind = False
        return self._backlog_kind

    async def _kind_clause(self, kind: str) -> str:
        """The `kind` filter, or nothing at all on a database that lacks it."""
        return f" AND kind = '{kind}'" if await self._has_backlog_kind() else ""

    async def site_exists(self, host: str) -> bool:
        """True when this host is already indexed (strict deduplication)."""
        cleaned = (host or "").strip().lower().removeprefix("www.")
        if not cleaned:
            return False
        client = get_client()
        try:
            row = await execute_safe("SELECT 1 FROM sites WHERE domain = ? LIMIT 1", [cleaned])
            return bool(getattr(row, "rows", ()))
        except Exception as exc:
            logger.debug("site probe failed for %s: %s", cleaned, exc)
            return False

    async def enqueue_domain(self, host: str, *, reason: str = "discovered") -> bool:
        """Add a discovered domain to the backlog. False when already queued."""
        cleaned = (host or "").strip().lower().removeprefix("www.")
        if not cleaned or not is_indexable(f"https://{cleaned}/"):
            return False
        if not await self._has_backlog_kind():
            # No `kind` column means no domain queue on this database; the site
            # table still works, so nothing is lost — it just is not queued.
            return False
        client = get_client()
        try:
            existing = await execute_safe(
                "SELECT status, processed_at FROM crawl_backlog WHERE query = ? AND kind = 'domain' LIMIT 1",
                [cleaned],
            )
            rows = getattr(existing, "rows", ()) or ()
            if rows:
                # A domain that was crawled once is not dead forever: if it
                # finished (or failed) longer than the cooldown ago, re-arm it so
                # the refill has a real source once the registry is exhausted.
                status = str(rows[0][0] or "").lower()
                processed_at = rows[0][1]
                if status in REARMABLE_STATUSES and _older_than(processed_at, self.settings.domain_rearm_cooldown_seconds):
                    await execute_safe(
                        "UPDATE crawl_backlog SET status = 'pending', processed_at = NULL, "
                        "hits = COALESCE(hits, 0) + 1 WHERE query = ? AND kind = 'domain'",
                        [cleaned],
                    )
                    logger.info("re-armed %s for crawling (was %s)", cleaned, status)
                    return True
                return False
            await execute_safe(
                "INSERT INTO crawl_backlog (query, kind, reason, hits, status, created_at) "
                "VALUES (?, 'domain', ?, 0, 'pending', CURRENT_TIMESTAMP)",
                [cleaned, reason],
            )
            return True
        except Exception as exc:
            logger.debug("domain enqueue failed for %s: %s", cleaned, exc)
            return False

    async def pending_domains(self, limit: int = 10) -> list[str]:
        client = get_client()
        if not await self._has_backlog_kind():
            return []
        result = await execute_safe(
            "SELECT query FROM crawl_backlog WHERE kind = 'domain' AND status = 'pending' "
            "ORDER BY created_at ASC LIMIT ?",
            [max(1, limit)],
        )
        return [str(row[0]) for row in getattr(result, "rows", ()) or ()]

    async def mark_domain_processed(self, host: str, *, status: str = "processed") -> None:
        client = get_client()
        if not await self._has_backlog_kind():
            return
        cleaned = (host or "").strip().lower().removeprefix("www.")
        await execute_safe(
            "UPDATE crawl_backlog SET status = ?, processed_at = CURRENT_TIMESTAMP "
            "WHERE query = ? AND kind = 'domain'",
            [status, cleaned],
        )
        # The update above may match nothing — a domain that was never queued, or
        # one whose row was removed. Silence there is what let a crawl spin.
        check = await execute_safe(
            "SELECT status FROM crawl_backlog WHERE query = ? AND kind = 'domain' LIMIT 1",
            [cleaned],
        )
        rows = getattr(check, "rows", ()) or ()
        if not rows:
            logger.warning(
                "mark_domain_processed matched no row for %s (status %s) — "
                "the domain will stay queued and be crawled again",
                cleaned or "(empty)", status,
            )
        elif str(rows[0][0] or "").lower() != status.lower():
            logger.warning(
                "mark_domain_processed did not take for %s: wanted %s, found %s",
                cleaned, status, rows[0][0],
            )

    async def pending_domain_count(self) -> int:
        if not await self._has_backlog_kind():
            return 0
        result = await execute_safe(
            "SELECT COUNT(*) FROM crawl_backlog WHERE kind = 'domain' AND status = 'pending'"
        )
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def enqueue_urls(self, urls: Iterable[str], reason: str) -> int:
        client = get_client()
        statements: list[tuple[str, list[Any]]] = []
        for url in urls:
            normalised = normalise_url(url)
            if not normalised or not is_indexable(normalised):
                continue
            statements.append(
                (
                    "INSERT INTO crawl_queue (url, domain, depth, reason, status, created_at) "
                    "VALUES (?, ?, 0, ?, 'pending', CURRENT_TIMESTAMP) ON CONFLICT(url) DO NOTHING",
                    [normalised, root_domain(normalised), reason],
                )
            )
        if not statements:
            return 0
        await client.batch(statements)
        return len(statements)


_store: IndexStore | None = None


def get_store(settings: EngineSettings | None = None) -> IndexStore:
    global _store
    if _store is None:
        _store = IndexStore(settings)
    return _store


def domain_root_url(url: str) -> str:
    parsed = urlparse(normalise_url(url))
    return f"https://{parsed.netloc}/" if parsed.netloc else ""


def merge_sections(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
    limit: int = 5,
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(existing) + list(incoming):
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(
            {
                "title": str(item.get("title") or "")[:80],
                "url": url[:300],
                "snippet": str(item.get("snippet") or "")[:120],
            }
        )
        if len(merged) >= limit:
            break
    return merged


__all__ = [
    "IndexStore",
    "get_store",
    "build_match_query",
    "passes_relevance",
    "query_tokens",
    "domain_root_url",
    "merge_sections",
]
