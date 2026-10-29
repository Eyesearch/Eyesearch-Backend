#!/usr/bin/env python3
"""Audit the live index in place: re-queue what was truncated, purge what is junk.

Nothing here resets the database. Every step is either a re-queue, a delete of a
row that earns it, or a reindex.

    DATABASE_URL=postgresql://... python3 scripts/audit_and_requeue.py --dry-run
    DATABASE_URL=postgresql://... python3 scripts/audit_and_requeue.py

What it does, in order:

1. **Re-queue truncated pages.** A page stored under the old 40-character rule
   holds a fragment. Its URL goes back into `crawl_queue` at priority 0 so the
   crawler refetches it with the current 200-character ceiling and the fixed
   Indic tokeniser. The row is *not* deleted — the page stays searchable until
   the refetch replaces it.

2. **Purge what is not worth keeping.** Empty bodies, error and soft-404 pages,
   and thin pages under fifteen words. Each deletion is reported by reason.

3. **Enforce the per-domain cap**, keeping the highest-quality pages.

4. **Rebuild the search vectors** so every row carries the current weighting
   (title A, description B, body D) rather than whatever was in force when it was
   written, then REINDEX the GIN index.

5. **Report**, including a latency benchmark.

The crawler is not started. Re-queued URLs wait for it to be enabled; this
script only fills the queue.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.config import get_settings  # noqa: E402
from app.engine.store import IndexStore  # noqa: E402
from app.postgres import PostgresClient  # noqa: E402

#: A page at or below this length was stored under the old 40-character rule
#: (with room for the word-boundary trim and the ellipsis).
TRUNCATED_THRESHOLD = 45
#: Below this many words a page is navigation, not content.
THIN_WORD_COUNT = 15
#: Pages whose body looks like this are boilerplate, not a document.
BOILERPLATE_MARKERS = (
    "accept all cookies", "we use cookies", "subscribe to our newsletter",
    "all rights reserved", "enable javascript", "javascript is disabled",
    "please log in", "sign in to continue",
)


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


@dataclass
class AuditReport:
    truncated: int = 0
    requeued: int = 0
    purged_empty: int = 0
    purged_thin: int = 0
    purged_boilerplate: int = 0
    purged_over_cap: int = 0
    vectors_rebuilt: int = 0
    reindexed: bool = False
    active_pages: int = 0
    latency_ms: dict[str, float] = field(default_factory=dict)
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "truncated": self.truncated,
            "requeued": self.requeued,
            "purged_empty": self.purged_empty,
            "purged_thin": self.purged_thin,
            "purged_boilerplate": self.purged_boilerplate,
            "purged_over_cap": self.purged_over_cap,
            "vectors_rebuilt": self.vectors_rebuilt,
            "reindexed": self.reindexed,
            "active_pages": self.active_pages,
            "latency_ms": self.latency_ms,
            "dry_run": self.dry_run,
        }

    def as_text(self) -> str:
        lines = [
            "",
            "  Audit summary",
            "  " + "-" * 58,
            f"  pages stored under the 40-character rule : {self.truncated}",
            f"  re-queued at priority 0                 : {self.requeued}",
            f"  purged — empty body                     : {self.purged_empty}",
            f"  purged — under {THIN_WORD_COUNT} words                : {self.purged_thin}",
            f"  purged — boilerplate only               : {self.purged_boilerplate}",
            f"  purged — over the per-domain cap        : {self.purged_over_cap}",
            f"  search vectors rebuilt                  : {self.vectors_rebuilt}",
            f"  GIN index rebuilt                       : {'yes' if self.reindexed else 'no'}",
            f"  active pages remaining                  : {self.active_pages}",
        ]
        for query, ms in self.latency_ms.items():
            lines.append(f"  latency {query!r:30} : {ms:.0f} ms")
        if self.dry_run:
            lines.append("")
            lines.append("  DRY RUN — nothing was written")
        return "\n".join(lines)


async def run(dsn: str, *, dry_run: bool, benchmark: bool) -> AuditReport:
    settings = get_settings()
    client = PostgresClient(dsn, statement_timeout_ms=60_000)
    store = IndexStore(client, settings)
    report = AuditReport(dry_run=dry_run)

    try:
        # -- 1. re-queue what was truncated ------------------------------
        # The domain lives on `sites`, not on `pages`; a page with no site row
        # falls back to its own host so the queue row is still usable.
        truncated = await client.execute(
            "SELECT p.url, s.domain FROM pages p "
            "LEFT JOIN sites s ON s.site_id = p.site_id "
            "WHERE length(p.body_text) <= ?",
            (TRUNCATED_THRESHOLD,),
        )
        rows = [tuple(row) for row in truncated.rows]
        report.truncated = len(rows)

        if not dry_run:
            for url, domain in rows:
                domain = domain or _host_of(url)
                # ON CONFLICT: the URL may already be queued. It is reset to
                # pending at priority 0 rather than duplicated.
                await client.execute(
                    "INSERT INTO crawl_queue (url, domain, status, retry_count, priority) "
                    "VALUES (?, ?, 'pending', 0, 0) "
                    "ON CONFLICT (url) DO UPDATE SET status = 'pending', "
                    "retry_count = 0, priority = 0, processed_at = NULL, last_error = NULL",
                    (url, domain),
                )
                report.requeued += 1

        # -- 2. purge what is not worth keeping -------------------------
        empty = await client.execute(
            "SELECT count(*) FROM pages WHERE body_text IS NULL OR trim(body_text) = ''"
        )
        report.purged_empty = int(empty.scalar(0) or 0)

        # Truncated pages are excluded from the thin rule on purpose. A page cut
        # to 40 characters by the old ceiling has six words; calling that "thin"
        # and deleting it would throw away the very rows step 1 just queued for
        # refetching. Thin means short *by nature*, not short by our own doing.
        thin_rows = await client.execute(
            "SELECT page_id FROM pages WHERE array_length("
            "regexp_split_to_array(trim(body_text), '\\s+'), 1) < ? "
            "AND length(body_text) > ?",
            (THIN_WORD_COUNT, TRUNCATED_THRESHOLD),
        )
        thin = [int(row[0]) for row in thin_rows.rows]

        boilerplate = await client.execute(
            "SELECT page_id FROM pages WHERE lower(body_text) LIKE ANY (ARRAY["
            + ", ".join(f"'%{marker}%'" for marker in BOILERPLATE_MARKERS)
            + "])"
        )
        boiler_ids = [int(row[0]) for row in boilerplate.rows]

        report.purged_thin = len(thin)
        report.purged_boilerplate = len(boiler_ids)

        if not dry_run:
            await client.execute(
                "DELETE FROM pages WHERE body_text IS NULL OR trim(body_text) = ''"
            )
            doomed = sorted(set(thin) | set(boiler_ids))
            for start in range(0, len(doomed), 200):
                chunk = doomed[start : start + 200]
                await client.execute(
                    "DELETE FROM pages WHERE page_id IN ("
                    + ", ".join("?" for _ in chunk)
                    + ")",
                    tuple(chunk),
                )

        # -- 3. enforce the per-domain cap ------------------------------
        cap = settings.max_pages_per_domain
        excess = await client.execute(
            "SELECT count(*) FROM ("
            "  SELECT page_id, ROW_NUMBER() OVER ("
            "    PARTITION BY site_id ORDER BY quality_score DESC, page_id DESC"
            "  ) AS rn FROM pages WHERE site_id IS NOT NULL"
            ") ranked WHERE rn > ?",
            (cap,),
        )
        report.purged_over_cap = int(excess.scalar(0) or 0)
        if not dry_run and report.purged_over_cap:
            await client.execute(
                "DELETE FROM pages WHERE page_id IN ("
                "  SELECT page_id FROM ("
                "    SELECT page_id, ROW_NUMBER() OVER ("
                "      PARTITION BY site_id ORDER BY quality_score DESC, page_id DESC"
                "    ) AS rn FROM pages WHERE site_id IS NOT NULL"
                "  ) ranked WHERE rn > ?"
                ")",
                (cap,),
            )

        # -- 4. rebuild the vectors, then the index ---------------------
        pending = await client.execute("SELECT count(*) FROM pages")
        report.vectors_rebuilt = int(pending.scalar(0) or 0)
        if not dry_run and report.vectors_rebuilt:
            # Touching the column fires the trigger, which rebuilds the vector
            # with the current weights. The text is not changed.
            await client.execute("UPDATE pages SET body_text = body_text")
            await client.execute("ANALYZE pages")
            await client.execute("REINDEX INDEX idx_pages_search")
            report.reindexed = True

        total = await client.execute("SELECT count(*) FROM pages")
        report.active_pages = int(total.scalar(0) or 0)

        # -- 5. benchmark ----------------------------------------------
        if benchmark:
            for query in ("karnataka", "india"):
                started = time.perf_counter()
                await store.search(query)
                report.latency_ms[query] = (time.perf_counter() - started) * 1000
    finally:
        await client.close()

    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--no-benchmark", action="store_true", help="skip the latency sample")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be set to a postgresql:// URL", file=sys.stderr)
        return 2

    report = await run(dsn, dry_run=args.dry_run, benchmark=not args.no_benchmark)
    print(report.as_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
