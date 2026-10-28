"""
03_parser_agent.py - HTML Parser Agent with Deep Recursion and Corrupt URL Shields.
Extracts title, meta description, clean text, and outbound links without crashing.
"""
import re
import logging
from typing import List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class ParsedPage(BaseModel):
    url: str
    title: str = Field(default="")
    description: str = Field(default="")
    text_content: str = Field(default="")
    outbound_links: List[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    is_boilerplate: bool = Field(default=False)

class ParserAgent:
    def __init__(self, max_outbound_links: int = 100):
        self.max_outbound_links = max_outbound_links
        self.non_navigational_schemes = ("mailto:", "tel:", "javascript:", "data:", "sms:", "#")
        self.ignored_extensions = (
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
            ".mp4", ".mp3", ".zip", ".tar", ".gz", ".docx", ".xlsx",
            ".pptx", ".css", ".js", ".json", ".xml", ".ico", ".woff", ".woff2"
        )

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        # Strip SQLite-breaking null bytes and condense whitespace
        clean = text.replace("\x00", "").strip()
        return re.sub(r"\s+", " ", clean)

    def extract_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        valid_links = []
        if not soup:
            return valid_links

        try:
            anchor_tags = soup.find_all("a", href=True)
        except Exception:
            return valid_links

        for tag in anchor_tags:
            try:
                href = tag.get("href")
                if not href or not isinstance(href, str):
                    continue

                href = href.strip()
                if not href or href.startswith(self.non_navigational_schemes):
                    continue

                # Critical Defense: Drop unbalanced square bracket IPv6 traps before urllib crashes
                if "[" in href and "]" not in href:
                    continue

                # Safe URL Resolution
                try:
                    absolute_url = urljoin(base_url, href)
                    parsed = urlsplit(absolute_url)
                except (ValueError, UnicodeError, Exception):
                    continue

                if parsed.scheme not in ("http", "https"):
                    continue

                if not parsed.netloc or "@" in parsed.netloc:
                    continue

                # Extension filter
                path_lower = parsed.path.lower()
                if any(path_lower.endswith(ext) for ext in self.ignored_extensions):
                    continue

                # Canonicalize URL without fragment
                clean_url = urlunsplit((
                    parsed.scheme,
                    parsed.netloc.lower(),
                    parsed.path,
                    parsed.query,
                    ""
                ))

                if clean_url not in valid_links:
                    valid_links.append(clean_url)

                if len(valid_links) >= self.max_outbound_links:
                    break

            except Exception:
                continue

        return valid_links

    def parse(self, url: str, html_content: str) -> ParsedPage:
        if not html_content or not isinstance(html_content, str):
            return ParsedPage(url=url)

        try:
            # Parse with recursion limit protection
            soup = BeautifulSoup(html_content[:1_500_000], "html.parser")
        except (RecursionError, Exception) as ex:
            logger.warning(f"Parser fallback activated on {url}: {ex}")
            try:
                soup = BeautifulSoup(html_content[:200_000], "html.parser")
            except Exception:
                return ParsedPage(url=url)

        # 1. Extract Title
        title = ""
        try:
            if soup.title and soup.title.string:
                title = self.clean_text(soup.title.string)
        except Exception:
            pass

        # 2. Extract Meta Description
        description = ""
        try:
            meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            if not meta:
                meta = soup.find("meta", attrs={"property": re.compile(r"^og:description$", re.I)})
            if meta and meta.get("content"):
                description = self.clean_text(str(meta.get("content")))
        except Exception:
            pass

        # 3. Clean and Extract Core Body Text
        for el in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside"]):
            el.decompose()

        try:
            raw_text = soup.get_text(separator=" ", strip=True)
            text_content = self.clean_text(raw_text)
        except Exception:
            text_content = ""

        # 4. Extract Outbound Links Safely
        outbound_links = self.extract_links(soup, url)

        return ParsedPage(
            url=url,
            title=title[:255],
            description=description[:500],
            text_content=text_content[:50000],
            outbound_links=outbound_links
        )
