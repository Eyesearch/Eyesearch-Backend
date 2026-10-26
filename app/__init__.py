"""EyeSearch Unified Search & Retrieval Gateway."""

from app.config import get_settings

__version__ = get_settings().app_version
__all__ = ["__version__"]
