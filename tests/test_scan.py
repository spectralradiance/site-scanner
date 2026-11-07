"""End-to-end test: serve tests/site locally and scan it."""

from __future__ import annotations

import functools
import random
import struct
import threading
import time
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from site_scanner.crawler import Scanner
from site_scanner.models import Config
from site_scanner.report import write_issues_csv, write_pages_csv

SITE_DIR = Path(__file__).parent / "site"


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def do_HEAD(self):
        if not self._dynamic(head=True):
            super().do_HEAD()

    def do_GET(self):
        if not self._dynamic(head=False):
            super().do_GET()

    def _send(self, status, ctype, body: bytes, head: bool, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _dynamic(self, head: bool) -> bool:
        base = f"http://{self.headers['Host']}"
        if self.path == "/robots.txt":
            body = f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n".encode()
            self._send(200, "text/plain", body, head)
            return True
        if self.path == "/sitemap.xml":
            urls = ["/", "/about.html", "/blog/post.html", "/orphan.html", "/gone.html", "/old-page",
                    "/private/secret.html"]
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(f"<url><loc>{base}{u}</loc></url>" for u in urls)
                    + "</urlset>").encode()
            self._send(200, "application/xml", body, head)
            return True
        if self.path == "/old-page":
            self._send(301, "text/html", b"", head, {"Location": "/about.html"})
            return True
        if self.path == "/slow.html":
            time.sleep(1.2)  # slow server response (TTFB)
            body = (b"<!doctype html><html lang='en'><head><meta name='viewport' content='width=device-width'>"
                    b"<title>Slow server page - Acme Widgets</title></head><body><main><h1>Slow</h1>"
                    b"<p>This page takes a long time to start responding.</p></main></body></html>")
            self._send(200, "text/html", body, head)
            return True
        if self.path == "/assets/heavy.js":
            self._send(200, "application/javascript", HEAVY_JS, head)
            return True
        if self.path == "/assets/big.png":
            self._send(200, "image/png", big_png(), head)
            return True
        return False


# ~350 KB of JavaScript that blocks the main thread for 400 ms shortly after load.
HEAVY_JS = (
    "/*" + "x" * 350_000 + "*/\n"
    "function heavyWork(){const end=performance.now()+400;while(performance.now()<end){}}\n"
    "setTimeout(heavyWork, 50);\n"
).encode()


@functools.lru_cache(maxsize=1)
def big_png() -> bytes:
    """A 500x500 noisy PNG (~750 KB) that compresses badly."""
    w = h = 500
    rnd = random.Random(42)
    raw = b"".join(b"\x00" + rnd.randbytes(w * 3) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def site_url():
    handler = functools.partial(Handler, directory=str(SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(scope="module")
def result(site_url):
    config = Config(start_url=site_url, workers=4, timeout=5)
    return Scanner(config, log=lambda m: None).run()


def find(result, issue, url_part=None, target_part=None):
    return [
        i for i in result.issues
        if i.issue == issue
        and (url_part is None or url_part in i.page_url)
        and (target_part is None or target_part in i.target)
    ]


def test_pages_crawled(result):
    urls = {p.final_url for p in result.pages if p.is_html}
    assert any(u.endswith("/about.html") for u in urls)
    assert any(u.endswith("/orphan.html") for u in urls)  # only reachable via sitemap
    assert any(u.endswith("/extra.html") for u in urls)
    assert not any("private" in u for u in urls)  # blocked by robots.txt


def test_broken_links(result):
    assert find(result, "Broken link", "/127.0.0.1", "missing.html")
    assert find(result, "Broken link", "about.html", "contact.html")
    assert find(result, "Broken link (external)", target_part="127.0.0.1:1/partner")
    assert find(result, "Broken image", target_part="hero.jpg")
    assert find(result, "Broken featured image (external)", "about.html")
    assert find(result, "Internal link redirects", target_part="old-page")


def test_seo_checks(result):
    assert find(result, "Title too long", "about.html")
    assert find(result, "Missing meta description", "about.html")
    assert find(result, "Meta description too short", "post.html")
    assert find(result, "Missing featured image (og:image)", "post.html")
    assert find(result, "Multiple H1 headings", "post.html")
    assert len(find(result, "Duplicate page title")) == 2


def test_content_checks(result):
    typos = {i.target for i in find(result, "Possible typo", "about.html")}
    assert {"recieve", "teh"} <= typos
    footer = find(result, "Possible typo (header/nav/footer)", target_part="reservd")
    assert len(footer) == 1 and "2 page(s)" in footer[0].details
    assert find(result, "Repeated word", "about.html")
    assert find(result, "Placeholder text (lorem ipsum)", "about.html")
    assert find(result, "Skipped heading level", "about.html")
    assert find(result, "Non-descriptive link text", "about.html")
    assert find(result, "Link has no text", "about.html")
    assert find(result, "Line breaks used for spacing", "about.html")
    assert find(result, "Low colour contrast", "about.html")
    assert find(result, "Image missing alt text", target_part="logo.png")
    assert find(result, "Missing lang attribute", "about.html")
    assert find(result, "Missing viewport meta tag", "about.html")


def test_sitemap_checks(result):
    assert find(result, "Orphan page (in sitemap but not linked)", "orphan.html")
    assert not find(result, "Orphan page (in sitemap but not linked)", "about.html")
    assert find(result, "Sitemap URL returns an error", "gone.html")
    assert find(result, "Sitemap URL redirects", "old-page")
    assert find(result, "Sitemap URL blocked by robots.txt", "private")
    assert find(result, "Page not in sitemap", "extra.html")
    assert not find(result, "Page not in sitemap", "orphan.html")


@pytest.fixture(scope="module")
def browser_result(site_url):
    pytest.importorskip("playwright")
    config = Config(start_url=site_url, workers=4, timeout=5, check_external=False, spelling=False,
                    render=True, speed=True, speed_profile="desktop")
    try:
        return Scanner(config, log=lambda m: None).run()
    except RuntimeError as exc:  # Chromium not installed
        pytest.skip(str(exc))


def page_metrics(result, url_part):
    return next(p.metrics for p in result.pages if p.final_url.endswith(url_part))


def test_browser_contrast(browser_result):
    details = " ".join(i.details for i in find(browser_result, "Low colour contrast", "extra.html"))
    assert "#c8c8c8 on #ffffff" in details      # colour from a stylesheet, not inline
    assert "#333333 on #1a1a1a" in details      # dark-on-dark banner
    assert find(browser_result, "Low colour contrast", "about.html")


def test_browser_javascript_seo(browser_result):
    r = browser_result
    assert find(r, "Title only set by JavaScript", "js.html")
    assert find(r, "Most content is loaded by JavaScript", "js.html")
    assert find(r, "Links only added by JavaScript", "js.html", "js-only-target.html")
    # the rendered title replaces the missing one, and JS-added links are link-checked
    assert not find(r, "Missing page title", "js.html")
    assert find(r, "Broken link", "js.html", "js-only-target.html")


def test_browser_errors_and_requests(browser_result):
    r = browser_result
    errors = find(r, "JavaScript error", "js.html")
    targets = " ".join(i.target for i in errors)
    assert "boom from test page" in targets
    assert "custom failure from test page" in targets
    assert find(r, "Failed request during page load", "js.html", "/api/missing.json")


def test_browser_accessibility_and_mobile(browser_result):
    r = browser_result
    axe = [i for i in r.issues if "axe rule 'label'" in i.details]
    assert axe and "js.html" in axe[0].page_url
    assert find(r, "Page scrolls sideways on mobile", "layout.html", "div.wide-table")
    assert find(r, "Tap targets too small", "layout.html", "button.tiny-btn")
    assert find(r, "Text too small on mobile", "layout.html")
    assert find(r, "Image much larger than displayed", "layout.html", "big.png")
    assert find(r, "Images without width/height", "layout.html", "big.png")
    assert find(r, "Offscreen images not lazy-loaded", "layout.html", "logo.png")


def test_server_response_time(browser_result):
    assert find(browser_result, "Slow server response (TTFB)", "slow.html")
    assert page_metrics(browser_result, "slow.html")["ttfb_ms"] >= 1000


def test_speed_metrics_and_causes(browser_result):
    r = browser_result
    m = page_metrics(r, "perf.html")
    assert m["lcp_ms"] and m["fcp_ms"] and m["weight_kb"] > 700
    tbt = find(r, "High Total Blocking Time (TBT)", "perf.html")
    assert tbt and "heavy.js" in tbt[0].details        # culprit script attributed
    cls = find(r, "Layout shift (CLS)", "perf.html")
    assert cls and m["cls"] > 0.1
    assert find(r, "Render-blocking resource", target_part="style.css")
    assert find(r, "Text file not compressed", target_part="heavy.js")
    cache = find(r, "Static files not cached long enough", target_part="script files")
    assert cache and "heavy.js" in cache[0].details
    assert find(r, "Large JavaScript file", target_part="heavy.js")
    assert find(r, "Image could use a modern format", target_part="big.png")
    assert find(r, "Site not served over HTTP/2")
    # site-wide causes are reported once, not per page
    assert len(find(r, "Render-blocking resource", target_part="style.css")) == 1


def test_csv_output(result, tmp_path):
    issues_csv = tmp_path / "issues.csv"
    pages_csv = tmp_path / "pages.csv"
    write_issues_csv(result, issues_csv)
    write_pages_csv(result, pages_csv)
    text = issues_csv.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "Page Title,URL,Severity,Category,Issue,Details,Target"
    assert "Broken link" in text
    pages = pages_csv.read_text(encoding="utf-8-sig")
    assert "orphan.html" in pages
