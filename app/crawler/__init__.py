"""Targeted crawler for Indian and regional domains.

Deliberately small: a seed list, robots.txt politeness, a byte cap and a page
budget per host. It is not a general web crawler and does not try to be one.
"""

from app.crawler.crawler import CrawlResult, crawl_seed, fetch_page
from app.crawler.seeds import SEED_DOMAINS

__all__ = ["CrawlResult", "crawl_seed", "fetch_page", "SEED_DOMAINS"]
