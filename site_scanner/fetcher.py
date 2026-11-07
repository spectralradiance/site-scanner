"""HTTP fetching with retries, politeness delay and a link-status cache."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import LinkStatus
from .urls import normalize_url


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    content: bytes = b""
    encoding: str | None = None
    error: str = ""
    elapsed_ms: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return "text/html" in ct or "application/xhtml" in ct


def _describe_error(exc: Exception) -> str:
    """Turn a requests exception into a short, human-readable reason."""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSL/TLS certificate error"
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return "Request timed out"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "Too many redirects (redirect loop?)"
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema)):
        return "Invalid URL"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "nameresolution" in low or "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
            return "DNS lookup failed (domain does not resolve)"
        if "refused" in low or "failed to establish a new connection" in low:
            return "Connection refused"
        if "reset" in low or "aborted" in low or "remotedisconnected" in low:
            return "Connection reset by server"
        return "Connection error"
    name = type(exc).__name__
    if len(msg) > 150:
        msg = msg[:150] + "..."
    return f"{name}: {msg}" if msg else name


class Fetcher:
    def __init__(self, user_agent: str, timeout: float = 15.0, delay: float = 0.0, pool_size: int = 16):
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        })
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            backoff_factor=0.5,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"HEAD", "GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._cache: dict[str, LinkStatus] = {}
        self._cache_lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    # -- politeness -------------------------------------------------------
    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        with self._throttle_lock:
            wait = self._last_request + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    # -- page fetch -------------------------------------------------------
    def get(self, url: str, max_bytes: int = 10 * 1024 * 1024, polite: bool = True) -> FetchResult:
        """GET a URL, following redirects. Body is only read for HTML/XML/text responses."""
        if polite:
            self._throttle()
        result = FetchResult(url=url)
        start = time.monotonic()
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            result.error = _describe_error(exc)
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            return result
        try:
            result.status = resp.status_code
            result.final_url = normalize_url(resp.url) or resp.url
            result.redirect_chain = [normalize_url(r.url) or r.url for r in resp.history]
            result.content_type = resp.headers.get("Content-Type", "")
            result.headers = dict(resp.headers)
            ct = result.content_type.lower()
            wants_body = (
                not ct or "html" in ct or "xml" in ct or ct.startswith("text/")
                or "gzip" in ct or "octet-stream" in ct
            )
            if wants_body:
                chunks, size = [], 0
                for chunk in resp.iter_content(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        break
                result.content = b"".join(chunks)
                if "charset=" in ct:
                    result.encoding = resp.encoding
        except requests.RequestException as exc:
            result.error = _describe_error(exc)
        finally:
            resp.close()
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result

    # -- link status check ------------------------------------------------
    def check(self, url: str, polite: bool = False) -> LinkStatus:
        """Return the HTTP status of ``url`` (HEAD, falling back to GET). Results are cached."""
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]
            event = self._inflight.get(url)
            owner = event is None
            if owner:
                event = threading.Event()
                self._inflight[url] = event
        if not owner:
            event.wait()
            with self._cache_lock:
                return self._cache[url]

        try:
            status = self._check_uncached(url, polite)
        except Exception as exc:  # never let one URL kill the scan
            status = LinkStatus(url=url, error=_describe_error(exc))
        with self._cache_lock:
            self._cache[url] = status
            self._inflight.pop(url, None)
        event.set()
        return status

    def seed_cache(self, status: LinkStatus) -> None:
        with self._cache_lock:
            self._cache.setdefault(status.url, status)

    def _check_uncached(self, url: str, polite: bool) -> LinkStatus:
        result = LinkStatus(url=url)
        resp = None
        if polite:
            self._throttle()
        try:
            resp = self.session.head(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            result.error = _describe_error(exc)
        # Many servers mishandle HEAD; retry with GET whenever HEAD did not succeed.
        if resp is None or resp.status_code >= 400:
            if resp is not None:
                resp.close()
            if polite:
                self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
                result.error = ""
            except requests.RequestException as exc:
                result.error = _describe_error(exc)
                return result
        try:
            result.status = resp.status_code
            result.final_url = normalize_url(resp.url) or resp.url
            result.content_type = resp.headers.get("Content-Type", "")
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                result.content_length = int(cl)
        finally:
            resp.close()
        return result
