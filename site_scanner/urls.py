"""URL normalisation and classification helpers."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

# Links to these file types are status-checked but never parsed as pages.
NON_HTML_EXTENSIONS = {
    "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "avif", "bmp", "ico", "tif", "tiff",
    "zip", "gz", "tar", "rar", "7z", "exe", "dmg", "msi",
    "mp3", "mp4", "m4a", "mov", "avi", "wmv", "webm", "ogg", "wav",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "rtf",
    "css", "js", "json", "xml", "rss", "atom",
    "woff", "woff2", "ttf", "otf", "eot",
}

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "callto:", "skype:", "whatsapp:", "ftp:")

_WS = re.compile(r"[\t\r\n]")


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return an absolute, canonical form of ``url`` (no fragment), or None if not http(s)."""
    if url is None:
        return None
    url = _WS.sub("", url).strip()
    if not url:
        return None
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def is_skippable_href(href: str | None) -> bool:
    if href is None:
        return True
    h = href.strip().lower()
    return h.startswith(SKIP_SCHEMES)


def is_placeholder_href(href: str | None) -> bool:
    if href is None:
        return False
    h = href.strip().lower()
    return h in ("", "#") or h.startswith("javascript:")


def host_key(url: str) -> tuple[str, int | None]:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = p.port
    if port in (80, 443):
        port = None
    return host, port


def is_internal(url: str, start_url: str, include_subdomains: bool = False) -> bool:
    h, port = host_key(url)
    sh, sport = host_key(start_url)
    if port != sport:
        return False
    if h == sh:
        return True
    return include_subdomains and h.endswith("." + sh)


def looks_like_page(url: str) -> bool:
    """Guess from the extension whether a URL is an HTML page worth crawling."""
    path = urlsplit(url).path
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return True
    ext = last.rsplit(".", 1)[-1].lower()
    return ext not in NON_HTML_EXTENSIONS
