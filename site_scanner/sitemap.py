"""robots.txt and XML sitemap discovery/parsing."""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from .fetcher import Fetcher
from .urls import normalize_url

FALLBACK_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml.gz")
MAX_SITEMAP_FILES = 200
MAX_SITEMAP_URLS = 50_000


@dataclass
class RobotsInfo:
    url: str
    found: bool = False
    status: int | None = None
    parser: RobotFileParser | None = None
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None

    def allowed(self, url: str, user_agent: str) -> bool:
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


@dataclass
class SitemapResult:
    urls: dict[str, str] = field(default_factory=dict)      # page URL -> sitemap file it came from
    files: list[str] = field(default_factory=list)          # sitemap files successfully parsed
    errors: list[tuple[str, str]] = field(default_factory=list)  # (sitemap url, problem)
    discovered_from: str = ""                                # "robots.txt", "user", "default location"


def load_robots(fetcher: Fetcher, start_url: str, user_agent: str) -> RobotsInfo:
    robots_url = urljoin(start_url, "/robots.txt")
    info = RobotsInfo(url=robots_url)
    res = fetcher.get(robots_url)
    info.status = res.status
    parser = RobotFileParser(robots_url)
    if res.status is None:
        parser = None
    elif res.status in (401, 403):
        parser.disallow_all = True
    elif res.status >= 400:
        parser.allow_all = True
    else:
        info.found = True
        text = res.content.decode("utf-8", errors="replace")
        parser.parse(text.splitlines())
        info.sitemaps = [s.strip() for s in (parser.site_maps() or [])]
        try:
            delay = parser.crawl_delay(user_agent)
            info.crawl_delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            info.crawl_delay = None
    info.parser = parser
    return info


def _decode(content: bytes) -> bytes:
    if content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def load_sitemaps(fetcher: Fetcher, start_url: str, robots: RobotsInfo, extra: list[str]) -> SitemapResult:
    result = SitemapResult()
    if extra:
        queue = list(extra)
        result.discovered_from = "user"
    elif robots.sitemaps:
        queue = list(robots.sitemaps)
        result.discovered_from = "robots.txt"
    else:
        queue = []
        for path in FALLBACK_SITEMAP_PATHS:
            candidate = urljoin(start_url, path)
            res = fetcher.get(candidate)
            if res.status == 200 and res.content and b"<" in _decode(res.content)[:2048]:
                queue = [candidate]
                result.discovered_from = "default location"
                break

    seen: set[str] = set()
    while queue and len(seen) < MAX_SITEMAP_FILES and len(result.urls) < MAX_SITEMAP_URLS:
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)
        res = fetcher.get(sm_url, max_bytes=50 * 1024 * 1024)
        if res.status is None:
            result.errors.append((sm_url, f"Could not fetch sitemap ({res.error})"))
            continue
        if res.status >= 400:
            result.errors.append((sm_url, f"Sitemap returned HTTP {res.status}"))
            continue
        try:
            root = ET.fromstring(_decode(res.content))
        except ET.ParseError as exc:
            result.errors.append((sm_url, f"Sitemap is not valid XML ({exc})"))
            continue
        result.files.append(sm_url)
        root_name = _local(root.tag)
        if root_name == "sitemapindex":
            for el in root.iter():
                if _local(el.tag) == "loc" and el.text:
                    child = el.text.strip()
                    if child and child not in seen:
                        queue.append(child)
        elif root_name == "urlset":
            for url_el in root:
                if _local(url_el.tag) != "url":
                    continue
                for el in url_el:
                    if _local(el.tag) == "loc" and el.text:
                        norm = normalize_url(el.text.strip())
                        if norm:
                            result.urls.setdefault(norm, sm_url)
                        else:
                            result.errors.append((sm_url, f"Invalid URL in sitemap: {el.text.strip()}"))
        else:
            result.errors.append((sm_url, f"Unexpected sitemap root element <{root_name}>"))
    return result
