"""
config.py - Core Engine Configuration with Pure 4.5GB Storage Cap and Global Crawl.
"""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Turso Database Connectivity
    turso_db_url: str = "https://eyesearch-db-eyesearch.aws-ap-south-1.turso.io"
    turso_auth_token: str = ""
    environment: str = "production"

    # Engine Storage Safety Ceilings (Pure 4.5 GB Hard Threshold)
    max_storage_mb: float = 4608.0  # 4.5 GB safe threshold on 5 GB tier
    freshness_threshold_days: int = 14
    estimated_kb_per_page: float = 12.0  # Average index footprint calculation

    # Network, Transport and Concurrency Limits
    crawler_enabled: bool = True
    crawl_batch_size: int = 60
    crawl_concurrency: int = 25
    crawl_timeout_seconds: float = 5.0
    max_download_bytes: int = 1_500_000  # 1.5MB strict boundary against OOM
    libsql_min_interval_seconds: float = 0.02

    # Discovery Breadth and International Rules
    max_outbound_links_per_page: int = 100
    max_pages_per_domain: int = 25
    allow_global_domains: bool = True  # Full worldwide indexing enabled

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
