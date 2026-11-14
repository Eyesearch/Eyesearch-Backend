"""One module per live source. Adding a source means adding a file here."""

from __future__ import annotations

from app.live.sources.bullion import BullionSource
from app.live.sources.news import NewsSource
from app.live.sources.weather import WeatherSource

#: Every source the feed agent runs, in order. A source that is down is skipped
#: by the agent, not removed from this list.
SOURCES = (BullionSource, WeatherSource, NewsSource)

__all__ = ["SOURCES", "BullionSource", "WeatherSource", "NewsSource"]
