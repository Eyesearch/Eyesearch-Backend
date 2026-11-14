"""Live facts: values that are true now and wrong in fifteen minutes.

Kept apart from the index on purpose. A crawled page is useful for months and a
price is useless in an hour, so the two cannot share a table, a TTL rule or a
storage ceiling. Nothing here imports the index, and `tests/test_separation.py`
fails the build if that ever stops being true.
"""

from __future__ import annotations

from app.live.store import fact, put_fact, expire, all_facts
from app.live.registry import record_ok, record_fail, source_health

__all__ = ["fact", "put_fact", "expire", "all_facts", "record_ok", "record_fail", "source_health"]
