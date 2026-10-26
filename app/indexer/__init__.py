"""Ingestion: crawled pages in, ranked FTS5 index out."""

from app.indexer.pipeline import crawl_and_index, index_documents, process_backlog

__all__ = ["crawl_and_index", "index_documents", "process_backlog"]
