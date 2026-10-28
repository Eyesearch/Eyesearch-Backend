"""
01_frontier_agent.py - Frontier Link Admission Agent.
Controls domain caps, URL sanity, and global admission without geo-blocking.
"""
import re
import urllib.parse
import logging
from typing import List
from app.engine.config import settings

logger = logging.getLogger(__name__)

class FrontierAgent:
    def __init__(self, db_client):
        self.db = db_client
        self.mode = "discovery"

    def set_mode(self, mode: str) -> None:
        if mode in ("discovery", "freshness"):
            self.mode = mode

    def can_admit_links(self) -> bool:
        return self.mode == "discovery"

    def is_valid_url(self, url: str) -> bool:
        if not url or len(url) > 2048:
            return False

        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https"):
                return False

            if not parsed.netloc or "@" in parsed.netloc:
                return False

            # Prevent infinite calendar and spam query loops
            if len(parsed.query) > 150 or parsed.query.count("&") > 4:
                return False

            # Exclude binary and media assets
            path = parsed.path.lower()
            if any(path.endswith(ext) for ext in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
                ".pdf", ".zip", ".tar", ".gz", ".rar", ".mp4", ".mp3",
                ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf"
            )):
                return False

            return True
        except Exception:
            return False

    async def filter_and_enqueue(self, current_url: str, outbound_links: List[str]) -> int:
        # Zero growth during Freshness Mode
        if not self.can_admit_links():
            return 0

        valid_urls = []
        for link in outbound_links[:settings.max_outbound_links_per_page]:
            try:
                resolved = urllib.parse.urljoin(current_url, link.strip())
                if self.is_valid_url(resolved):
                    valid_urls.append(resolved)
            except Exception:
                continue

        if not valid_urls:
            return 0

        inserted_count = 0
        for target_url in set(valid_urls):
            try:
                domain = urllib.parse.urlsplit(target_url).netloc.lower()

                # Enforce global per-domain breadth cap (25 pages)
                domain_check = await self.db.execute(
                    "SELECT COUNT(*) FROM pages WHERE domain = ?", (domain,)
                )
                count = domain_check.rows[0][0] if domain_check.rows else 0

                if count < settings.max_pages_per_domain:
                    await self.db.execute(
                        """
                        INSERT OR IGNORE INTO crawl_queue (url, domain, status, created_at)
                        VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)
                        """,
                        (target_url, domain)
                    )
                    inserted_count += 1
            except Exception as e:
                logger.warning(f"Frontier admission skipped for {target_url}: {e}")
                continue

        return inserted_count
