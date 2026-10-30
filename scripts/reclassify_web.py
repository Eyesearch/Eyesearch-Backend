"""Re-run the classifier over pages currently sitting in WEB.

A page's category is decided at crawl time and stored. When the classifier
improves — new path rules, new host signals, a wider taxonomy — the pages already
in the index keep the answer the old classifier gave them. This re-derives it
from what the row already holds: the URL, the title and the description. No
network, no re-crawl.

Pages are only ever moved *out* of WEB. A page that has a real category keeps it,
because a stored answer from a page's own text is not made worse by being left
alone, and reclassifying the whole corpus is a much larger change than this.

Run it with --dry-run first.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import load_agent  # noqa: E402
from app.postgres import PostgresClient  # noqa: E402

BATCH = 500
WEB = "WEB"


async def reclassify(client: PostgresClient, *, dry_run: bool, limit: int | None) -> dict:
    module = load_agent("07_classifier_agent")
    classifier = module.ClassifierAgent()
    rule_to_code = {rule: code for code, rule in module.CODE_TO_RULE.items()}

    rows = await client.execute(
        "SELECT p.page_id, p.url, p.title, p.description FROM pages p "
        "JOIN categories c ON c.id = p.category_id WHERE c.code = ? "
        f"ORDER BY p.page_id LIMIT {int(limit) if limit else 1000000}",
        (WEB,),
    )

    codes = await client.execute("SELECT id, code FROM categories")
    id_by_code = {str(code): int(row_id) for row_id, code in codes.rows}

    moved: Counter[str] = Counter()
    updates: list[tuple[int, int]] = []
    for page_id, url, title, description in rows.rows:
        result = classifier.classify(str(url or ""), str(title or ""), str(description or ""))
        code = rule_to_code.get(result.category, WEB)
        if code == WEB:
            continue
        moved[code] += 1
        updates.append((id_by_code[code], int(page_id)))

    if not dry_run and updates:
        for start in range(0, len(updates), BATCH):
            chunk = updates[start : start + BATCH]
            await client.batch(
                [
                    ("UPDATE pages SET category_id = ? WHERE page_id = ?", (category_id, page_id))
                    for category_id, page_id in chunk
                ]
            )

    return {
        "examined": len(rows.rows),
        "reclassified": len(updates),
        "by_category": dict(sorted(moved.items())),
        "dry_run": dry_run,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--limit", type=int, default=None, help="cap the rows examined")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")

    client = PostgresClient(dsn, statement_timeout_ms=120_000)
    try:
        report = await reclassify(client, dry_run=args.dry_run, limit=args.limit)
        print(f"  examined      {report['examined']}")
        print(f"  reclassified  {report['reclassified']}")
        for code, count in report["by_category"].items():
            print(f"    -> {code:8} {count}")
        print("  dry run — nothing written" if args.dry_run else "  applied")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
