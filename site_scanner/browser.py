"""Browser pass (Playwright): checks the page as a visitor's browser sees it, after JavaScript runs.

For every HTML page this loads the page once in headless Chromium at desktop size and:
* re-runs all the on-page checks against the rendered DOM, and reports what JavaScript changed
  (title/description/canonical set by JS, content or links that only exist after JS);
* records JavaScript errors and failed network requests;
* runs an axe-core accessibility audit and the colour-contrast check;
* checks images (oversized for their display size, no dimensions, not lazy-loaded below the fold);
* resizes to phone width and checks for sideways scrolling, small tap targets and tiny text;
* optionally saves desktop and mobile screenshots.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checks import analyze_page
from .contrast import RENDER_JS, group_contrast
from .fetcher import FetchResult
from .models import ERROR, NOTICE, WARNING, Config, Issue, Page, SiteWideIssues
from .spelling import SpellCheck
from .urls import normalize_url

INSTALL_HINT = (
    "Browser checks need Playwright and Chromium:\n"
    "    pip install -e .[browser]\n"
    "    python -m playwright install chromium"
)

# Rules that duplicate checks the scanner already does itself.
AXE_DISABLED_RULES = ["color-contrast", "image-alt", "link-name", "html-has-lang", "target-size"]
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
AXE_SEVERITY = {"critical": ERROR, "serious": WARNING, "moderate": NOTICE, "minor": NOTICE}

TTFB_GOOD_MS = 800
TTFB_POOR_MS = 1800
DOM_SIZE_LIMIT = 1500
MOBILE_VIEWPORT = {"width": 375, "height": 812}
DESKTOP_VIEWPORT = {"width": 1366, "height": 900}

_SEL_JS = r"""
const __sel = (el) => {
  if (!el || el.nodeType !== 1) return '';
  let s = el.tagName.toLowerCase();
  if (el.id) return s + '#' + el.id;
  const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
  return cls.length ? s + '.' + cls.join('.') : s;
};
"""

PAGE_INFO_JS = "() => {" + _SEL_JS + r"""
  const nav = performance.getEntriesByType('navigation')[0];
  const vh = window.innerHeight;
  const oversized = [], noDims = [], notLazy = [];
  for (const img of document.images) {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith('data:')) continue;
    const r = img.getBoundingClientRect();
    const visible = r.width > 1 && r.height > 1;
    if (!visible) continue;
    const dpr = window.devicePixelRatio || 1;
    if (img.complete && img.naturalWidth > 400 && img.naturalWidth > r.width * dpr * 2.5) {
      oversized.push({ src, natural: img.naturalWidth + 'x' + img.naturalHeight,
                       shown: Math.round(r.width) + 'x' + Math.round(r.height) });
    }
    const hasAttrs = img.hasAttribute('width') && img.hasAttribute('height');
    const ar = getComputedStyle(img).aspectRatio;
    const inlineSized = img.style.width && img.style.height;
    if (!hasAttrs && !inlineSized && (!ar || ar === 'auto')) noDims.push(src);
    const top = r.top + window.scrollY;
    if (top > vh * 1.5 && img.loading !== 'lazy') notLazy.push(src);
  }
  return {
    // Server response time: request sent -> first byte (excludes DNS/connection setup and redirects)
    ttfb: nav && nav.responseStart ? Math.round(nav.responseStart - nav.requestStart) : null,
    domSize: document.getElementsByTagName('*').length,
    oversized, noDims, notLazy,
  };
}"""

MOBILE_JS = "() => {" + _SEL_JS + r"""
  const vw = document.documentElement.clientWidth;
  const sw = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const overflow = [];
  if (sw > vw + 1 && document.body) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.right <= vw + 1) continue;
      const p = el.parentElement;
      const pr = p ? p.getBoundingClientRect() : null;
      if (!pr || pr.right <= vw + 1 || p === document.body) {
        overflow.push(__sel(el) + ' (' + Math.round(r.width) + 'px wide)');
        if (overflow.length >= 5) break;
      }
    }
  }
  const visible = (el, cs) => cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) !== 0;
  const small = []; let smallCount = 0;
  const targets = document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [onclick]');
  for (const el of targets) {
    const cs = getComputedStyle(el);
    if (!visible(el, cs)) continue;
    if (el.tagName === 'A' && cs.display === 'inline') continue;  // links within text are exempt
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;                 // screen-reader-only
    if (r.width < 24 || r.height < 24) {
      smallCount++;
      if (small.length < 5) {
        const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 30);
        small.push(__sel(el) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + 'px' + (label ? ' "' + label + '"' : ''));
      }
    }
  }
  const tiny = []; let tinyCount = 0; const seen = new Set();
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const t = walker.currentNode;
      const text = t.textContent.replace(/\s+/g, ' ').trim();
      const el = t.parentElement;
      if (!text || !el || seen.has(el)) continue;
      seen.add(el);
      if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION'].includes(el.tagName)) continue;
      const cs = getComputedStyle(el);
      if (!visible(el, cs)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue;
      const size = parseFloat(cs.fontSize);
      if (size < 12) {
        tinyCount++;
        if (tiny.length < 3) tiny.push(Math.round(size * 10) / 10 + 'px "' + text.slice(0, 40) + '"');
      }
    }
  }
  return { viewport: vw, scrollWidth: sw, overflow, smallCount, small, tinyCount, tiny };
}"""

AXE_RUN_JS = r"""
async ([tags, disabled]) => {
  const rules = {};
  for (const r of disabled) rules[r] = { enabled: false };
  const res = await axe.run(document, { runOnly: { type: 'tag', values: tags }, rules, resultTypes: ['violations'] });
  return res.violations.map(v => ({
    id: v.id, impact: v.impact || 'moderate', help: v.help, url: v.helpUrl, count: v.nodes.length,
    targets: v.nodes.slice(0, 5).map(n => [].concat(n.target).join(' ')),
  }));
}
"""


@dataclass
class BrowserResult:
    url: str
    html: str = ""
    error: str = ""
    js_errors: list[tuple[str, str]] = field(default_factory=list)          # (kind, message)
    failed_requests: list[tuple[str, str, str]] = field(default_factory=list)  # (url, reason, type)
    axe: list[dict] = field(default_factory=list)
    axe_error: str = ""
    contrast: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    mobile: dict = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)


def _axe_path() -> Path | None:
    try:
        import axe_playwright_python
    except ImportError:
        return None
    path = Path(axe_playwright_python.__file__).parent / "axe.min.js"
    return path if path.exists() else None


def _slug(url: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", url.split("://", 1)[-1]).strip("-")[:80]
    return f"{base}-{hashlib.sha1(url.encode()).hexdigest()[:6]}"


def _short_error(exc: Exception) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:200]}"


async def _audit(context_factory, url: str, config: Config, axe_path: Path | None) -> BrowserResult:
    result = BrowserResult(url=url)
    context = await context_factory()
    page = await context.new_page()

    def on_console(msg):
        if msg.type == "error":
            text = msg.text
            if text.startswith(("Failed to load resource", "Mixed Content:")):
                return  # covered by failed-request and mixed-content checks
            loc = msg.location or {}
            where = loc.get("url", "")
            if where and loc.get("lineNumber") is not None:
                where += f":{loc['lineNumber'] + 1}"
            result.js_errors.append(("console.error", text + (f" (at {where})" if where else "")))

    def on_page_error(exc):
        result.js_errors.append(("Uncaught exception", str(exc)))

    def on_request_failed(req):
        reason = req.failure or "failed"
        if "ERR_ABORTED" in reason:
            return
        result.failed_requests.append((req.url, reason, req.resource_type))

    def on_response(resp):
        if resp.status >= 400:
            result.failed_requests.append((resp.url, f"HTTP {resp.status}", resp.request.resource_type))

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="load", timeout=max(config.timeout, 10) * 2000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)
        result.html = await page.content()
        result.info = await page.evaluate(PAGE_INFO_JS)
        result.contrast = group_contrast(await page.evaluate(RENDER_JS))
        if axe_path is not None:
            try:
                await page.add_script_tag(path=str(axe_path))
                result.axe = await page.evaluate(AXE_RUN_JS, [AXE_TAGS, AXE_DISABLED_RULES])
            except Exception as exc:  # e.g. blocked by Content-Security-Policy
                result.axe_error = _short_error(exc)
        shots = Path(config.screenshots_dir) if config.screenshots_dir else None
        if shots:
            path = shots / f"{_slug(url)}-desktop.png"
            await page.screenshot(path=str(path), full_page=True)
            result.screenshots.append(str(path))
        await page.set_viewport_size(MOBILE_VIEWPORT)
        await page.wait_for_timeout(400)
        result.mobile = await page.evaluate(MOBILE_JS)
        if shots:
            path = shots / f"{_slug(url)}-mobile.png"
            await page.screenshot(path=str(path), full_page=True)
            result.screenshots.append(str(path))
    except Exception as exc:
        result.error = _short_error(exc)
    finally:
        await context.close()
    # The page itself was fetched successfully by the crawler; ignore it here.
    result.failed_requests = [f for f in result.failed_requests if normalize_url(f[0]) != url]
    return result


async def _run(urls: list[str], config: Config, log: Callable[[str], None]) -> dict[str, BrowserResult]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    axe_path = _axe_path()
    if axe_path is None:
        log("axe-core not installed (pip install axe-playwright-python); skipping accessibility audit")
    if config.screenshots_dir:
        Path(config.screenshots_dir).mkdir(parents=True, exist_ok=True)

    results: dict[str, BrowserResult] = {}
    sem = asyncio.Semaphore(max(1, config.browser_workers))
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            raise RuntimeError(f"Could not start Chromium ({_short_error(exc)}).\n{INSTALL_HINT}") from exc

        async def factory():
            return await browser.new_context(user_agent=config.user_agent, viewport=DESKTOP_VIEWPORT)

        done = 0

        async def one(u: str) -> None:
            nonlocal done
            async with sem:
                results[u] = await _audit(factory, u, config, axe_path)
            done += 1
            if done % 10 == 0 or done == len(urls):
                log(f"  browser-checked {done}/{len(urls)} pages")

        await asyncio.gather(*(one(u) for u in urls))
        await browser.close()
    return results


def run_browser_audit(urls: list[str], config: Config, log: Callable[[str], None]) -> dict[str, BrowserResult]:
    return asyncio.run(_run(urls, config, log))


# ----------------------------------------------------------------------------
# Turning results into issues
# ----------------------------------------------------------------------------

def _compare_and_apply(page: Page, br: BrowserResult, config: Config, spell: SpellCheck | None,
                       headers: dict) -> list[Issue]:
    """Re-analyse the rendered DOM, replace the page's data with it, and report JS-dependent SEO."""
    rendered = Page(url=page.url, final_url=page.final_url, status=page.status, is_html=True)
    fake = FetchResult(url=page.final_url, final_url=page.final_url, status=page.status,
                       content_type="text/html", content=br.html.encode("utf-8"), encoding="utf-8",
                       headers=headers)
    analyze_page(rendered, fake, config, spell)

    url, title = page.final_url, rendered.title or page.title
    out: list[Issue] = []

    def add(issue: str, severity: str, details: str, target: str = "") -> None:
        out.append(Issue(url, title, "JavaScript SEO", issue, severity, details, target))

    for label, raw_v, ren_v in (("Title", page.title, rendered.title),
                                ("Meta description", page.meta_description, rendered.meta_description),
                                ("Canonical", page.canonical, rendered.canonical)):
        if ren_v and not raw_v:
            add(f"{label} only set by JavaScript", WARNING,
                f"The HTML has no {label.lower()}; JavaScript adds \"{ren_v[:120]}\". "
                "Crawlers that don't run JavaScript won't see it.")
        elif raw_v and not ren_v:
            add(f"{label} removed by JavaScript", WARNING, f"The HTML has \"{raw_v[:120]}\" but the rendered page has none.")
        elif raw_v and ren_v and raw_v != ren_v:
            add(f"{label} changed by JavaScript", WARNING if label == "Canonical" else NOTICE,
                f"HTML: \"{raw_v[:100]}\"  Rendered: \"{ren_v[:100]}\"")
    if rendered.noindex != page.noindex:
        add("Robots noindex changed by JavaScript", WARNING,
            f"noindex is {'on' if page.noindex else 'off'} in the HTML but {'on' if rendered.noindex else 'off'} after JavaScript.")
    if rendered.h1_count and not page.h1_count:
        add("H1 only added by JavaScript", NOTICE, "The HTML has no <h1>; JavaScript adds one.")
    if rendered.word_count > max(page.word_count * 2, page.word_count + 100):
        add("Most content is loaded by JavaScript", WARNING,
            f"{page.word_count} words in the HTML vs {rendered.word_count} after JavaScript runs. "
            "Search engines may index less of this page.")
    raw_links = {l.url for l in page.links if l.kind == "a"}
    js_links = sorted({l.url for l in rendered.links if l.kind == "a"} - raw_links)
    if js_links:
        add("Links only added by JavaScript", NOTICE,
            f"{len(js_links)} link(s) exist only after JavaScript runs; crawlers may not follow them.",
            " | ".join(js_links[:5]) + (" ..." if len(js_links) > 5 else ""))

    # Rendered data replaces the raw-HTML analysis.
    page.title = rendered.title
    page.meta_description = rendered.meta_description
    page.canonical = rendered.canonical
    page.noindex = rendered.noindex
    page.h1_count = rendered.h1_count
    page.word_count = rendered.word_count
    page.og_image = rendered.og_image
    page.links = rendered.links
    page.template_typos = rendered.template_typos
    page.issues = rendered.issues + out
    return out


def apply_browser_result(page: Page, br: BrowserResult, config: Config, spell: SpellCheck | None,
                         headers: dict, agg: SiteWideIssues) -> None:
    if br.error:
        page.issues.append(Issue(page.final_url, page.title, "Technical", "Page could not be rendered", WARNING,
                                 f"Browser checks skipped: {br.error}"))
        return
    _compare_and_apply(page, br, config, spell, headers)
    url, title = page.final_url, page.title

    def site(category, issue, severity, details, target, key_extra=""):
        agg.add(url, title, category, issue, severity, details, target if not key_extra else target + key_extra)

    # JavaScript errors (same error on many pages is reported once)
    seen = set()
    for kind, msg in br.js_errors:
        msg_one = " ".join(msg.split())[:300]
        if msg_one in seen:
            continue
        seen.add(msg_one)
        agg.add(url, title, "JavaScript", "JavaScript error", ERROR if kind == "Uncaught exception" else WARNING,
                f"{kind}.", msg_one)

    # Failed requests not already covered by the link checker
    linked = {l.url for l in page.links}
    for req_url, reason, rtype in br.failed_requests:
        norm = normalize_url(req_url) or req_url
        if norm in linked:
            continue
        if norm.endswith("/favicon.ico"):
            agg.add(url, title, "Technical", "Missing favicon", NOTICE, f"Browser request for favicon failed ({reason}).", norm)
            continue
        agg.add(url, title, "Links", "Failed request during page load", ERROR if rtype in ("script", "stylesheet") else WARNING,
                f"{reason} ({rtype} requested while the page loaded).", norm)

    # axe-core accessibility audit
    for v in br.axe:
        targets = " | ".join(v["targets"])
        agg.add(url, title, "Accessibility", v["help"], AXE_SEVERITY.get(v["impact"], NOTICE),
                f"axe rule '{v['id']}' ({v['impact']}): {v['count']} element(s) on the page. {v['url']}", targets)
    if br.axe_error:
        page.issues.append(Issue(url, title, "Technical", "Accessibility audit could not run", NOTICE, br.axe_error))

    # Colour contrast (computed styles)
    for f in br.contrast:
        agg.add(url, title, "Accessibility", "Low colour contrast", WARNING,
                f"Ratio {f['ratio']}:1 (WCAG AA needs {f['required']}:1). Text {f['fg']} on {f['bg']}, "
                f"{f['count']} element(s), e.g. \"{f['text']}\".", f"{f['selector']} ({f['fg']} on {f['bg']})")

    # Images
    info = br.info or {}
    for img in info.get("oversized", []):
        agg.add(url, title, "Images", "Image much larger than displayed", NOTICE,
                f"Image is {img['natural']}px but displayed at {img['shown']}px; serve a smaller version or use srcset.",
                img["src"])
    no_dims = list(dict.fromkeys(info.get("noDims", [])))
    if no_dims:
        agg.add(url, title, "Performance", "Images without width/height", NOTICE,
                f"{len(no_dims)} image(s) have no width/height attributes or CSS aspect-ratio, so the layout "
                "jumps as they load (CLS).", " | ".join(no_dims[:5]) + (" ..." if len(no_dims) > 5 else ""))
    not_lazy = list(dict.fromkeys(info.get("notLazy", [])))
    if not_lazy:
        agg.add(url, title, "Performance", "Offscreen images not lazy-loaded", NOTICE,
                f"{len(not_lazy)} image(s) well below the first screen load immediately; add loading=\"lazy\" so "
                "they don't compete with visible content.", " | ".join(not_lazy[:5]) + (" ..." if len(not_lazy) > 5 else ""))

    ttfb = info.get("ttfb")
    page.metrics["ttfb_ms"] = ttfb
    page.metrics["dom_size"] = info.get("domSize")
    if ttfb is not None and ttfb > TTFB_GOOD_MS:
        page.issues.append(Issue(url, title, "Performance", "Slow server response (TTFB)",
                                 WARNING if ttfb > TTFB_POOR_MS else NOTICE,
                                 f"The server took {ttfb} ms to start sending the page after the request "
                                 f"(good ≤ {TTFB_GOOD_MS} ms, poor > {TTFB_POOR_MS} ms; excludes connection setup). "
                                 "Look at server processing time, database queries, page caching or hosting."))
    dom = info.get("domSize") or 0
    if dom > DOM_SIZE_LIMIT:
        page.issues.append(Issue(url, title, "Performance", "Large DOM", NOTICE,
                                 f"{dom} HTML elements (recommended under {DOM_SIZE_LIMIT}); large pages render and update slowly."))

    # Mobile layout
    m = br.mobile or {}
    if m.get("scrollWidth", 0) > m.get("viewport", 10**6) + 1:
        targets = " | ".join(m.get("overflow", []))
        page.issues.append(Issue(url, title, "Mobile", "Page scrolls sideways on mobile", WARNING,
                                 f"At {m['viewport']}px wide the page is {m['scrollWidth']}px wide.", targets))
    if m.get("smallCount"):
        site("Mobile", "Tap targets too small", NOTICE,
             f"{m['smallCount']} button(s)/link(s) smaller than 24x24px at phone width (WCAG 2.2 target size).",
             " | ".join(m.get("small", [])))
    if m.get("tinyCount"):
        site("Mobile", "Text too small on mobile", NOTICE,
             f"{m['tinyCount']} text element(s) under 12px at phone width.", " | ".join(m.get("tiny", [])))
    page.screenshots = br.screenshots
