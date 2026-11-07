"""Page speed (Playwright + Chrome DevTools Protocol).

Each page is loaded in a fresh browser context (empty cache) with network and CPU throttling that
mimics Lighthouse's profiles, one page at a time so measurements don't compete for CPU.
Collected: FCP, LCP (and its element), CLS (and the elements that moved), Total Blocking Time
(and the scripts responsible, via the Long Animation Frames API), and every network request with its
size, type, compression, caching headers and protocol. These are turned into page-level metric issues
and site-wide "cause" issues (render-blocking files, uncompressed files, poor caching, legacy image
formats, heavy JavaScript and third parties).
"""

from __future__ import annotations

import asyncio
import re
import statistics
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from .models import NOTICE, WARNING, Config, Issue, Page, SiteWideIssues

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")

PROFILES = {
    # Matches Lighthouse's default mobile profile: slow 4G, 4x CPU slowdown.
    "mobile": {
        "context": {"viewport": {"width": 412, "height": 823}, "device_scale_factor": 1.75,
                    "is_mobile": True, "has_touch": True},
        "mobile_ua": True,
        "latency": 150, "down_kbps": 1638.4, "up_kbps": 750, "cpu": 4,
        "label": "emulated mobile (slow 4G, 4x CPU slowdown)",
    },
    "desktop": {
        "context": {"viewport": {"width": 1350, "height": 940}, "device_scale_factor": 1},
        "mobile_ua": False,
        "latency": 40, "down_kbps": 10240, "up_kbps": 10240, "cpu": 1,
        "label": "emulated desktop (fast connection)",
    },
}

# Google's Core Web Vitals / Lighthouse thresholds: (good, poor)
THRESHOLDS = {
    "lcp_ms": (2500, 4000),
    "fcp_ms": (1800, 3000),
    "cls": (0.1, 0.25),
    "tbt_ms": (200, 600),
}

PAGE_WEIGHT_NOTICE_KB = 2048
PAGE_WEIGHT_WARNING_KB = 4096
MAX_REQUESTS = 100
TOTAL_JS_LIMIT_KB = 1024
LARGE_SCRIPT_KB = 300
LEGACY_IMAGE_MIN_KB = 100
COMPRESS_MIN_BYTES = 2048
MIN_CACHE_SECONDS = 7 * 24 * 3600
MAX_FONT_FILES = 4
THIRD_PARTY_SHARE = 0.4
THIRD_PARTY_MIN_KB = 300

TEXT_MIME = re.compile(r"(text/|javascript|json|xml|svg)", re.I)
LEGACY_IMAGE_MIME = re.compile(r"image/(jpeg|jpg|png|gif|bmp)", re.I)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "nhs", "sch"}

PERF_INIT_JS = r"""
(() => {
  const s = window.__ssPerf = { lcp: null, lcpEl: null, shifts: [], longtasks: [], loaf: [], fcp: null };
  const desc = (el) => {
    if (!el || el.nodeType !== 1) return '';
    let d = el.tagName.toLowerCase();
    if (el.id) return d + '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
    return cls.length ? d + '.' + cls.join('.') : d;
  };
  const obs = (type, cb) => {
    try { new PerformanceObserver(list => list.getEntries().forEach(cb)).observe({ type, buffered: true }); } catch (e) {}
  };
  obs('paint', e => { if (e.name === 'first-contentful-paint') s.fcp = e.startTime; });
  obs('largest-contentful-paint', e => {
    const el = e.element;
    s.lcp = e.startTime;
    s.lcpEl = {
      selector: desc(el), url: e.url || '',
      lazy: !!(el && el.getAttribute && el.getAttribute('loading') === 'lazy'),
      text: el && !e.url ? (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60) : '',
    };
  });
  obs('layout-shift', e => {
    if (!e.hadRecentInput) s.shifts.push({ t: e.startTime, v: e.value, src: (e.sources || []).map(x => desc(x.node)).filter(Boolean) });
  });
  obs('longtask', e => s.longtasks.push({ t: e.startTime, d: e.duration }));
  obs('long-animation-frame', e => s.loaf.push({
    scripts: (e.scripts || []).map(x => ({ src: x.sourceURL || x.invoker || '', d: x.duration })),
  }));
})();
"""

PERF_COLLECT_JS = r"""
() => {
  const s = window.__ssPerf || { shifts: [], longtasks: [], loaf: [] };
  const nav = performance.getEntriesByType('navigation')[0] || {};
  let cls = 0, win = 0, winStart = -1e9, last = -1e9, winSrc = [], best = [];
  for (const sh of s.shifts) {
    if (sh.t - last > 1000 || sh.t - winStart > 5000) { win = 0; winStart = sh.t; winSrc = []; }
    win += sh.v; winSrc.push(...sh.src); last = sh.t;
    if (win > cls) { cls = win; best = winSrc.slice(); }
  }
  const fcp = s.fcp || 0;
  let tbt = 0, longTasks = 0;
  for (const t of s.longtasks) { if (t.t + t.d > fcp) { tbt += Math.max(0, t.d - 50); longTasks++; } }
  const scripts = {};
  for (const f of s.loaf) for (const sc of f.scripts) {
    if (sc.src && sc.d > 0) scripts[sc.src] = (scripts[sc.src] || 0) + sc.d;
  }
  const blocking = performance.getEntriesByType('resource')
    .filter(r => r.renderBlockingStatus === 'blocking')
    .map(r => ({ url: r.name, ms: Math.round(r.duration) }));
  return {
    fcp_ms: s.fcp, lcp_ms: s.lcp, lcp_el: s.lcpEl, cls: Math.round(cls * 1000) / 1000,
    cls_sources: [...new Set(best)].slice(0, 5), tbt_ms: Math.round(tbt), long_tasks: longTasks,
    scripts: Object.entries(scripts).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([u, d]) => [u, Math.round(d)]),
    load_ms: nav.loadEventEnd ? Math.round(nav.loadEventEnd) : null,
    blocking,
  };
}
"""


@dataclass
class Request:
    url: str
    type: str = ""
    status: int | None = None
    mime: str = ""
    headers: dict = field(default_factory=dict)
    size: int = 0
    protocol: str = ""
    failed: bool = False


@dataclass
class SpeedResult:
    url: str
    runs: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)      # medians
    requests: list[Request] = field(default_factory=list)
    error: str = ""


def site_domain(host: str) -> str:
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _short_error(exc: Exception) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:200]}"


async def _measure_once(browser, url: str, config: Config, profile: dict) -> tuple[dict, list[Request]]:
    ctx_args = dict(profile["context"])
    ua = MOBILE_UA if profile["mobile_ua"] else None
    ctx_args["user_agent"] = f"{ua} {config.user_agent}" if ua else config.user_agent
    context = await browser.new_context(**ctx_args)
    try:
        page = await context.new_page()
        await page.add_init_script(PERF_INIT_JS)
        cdp = await context.new_cdp_session(page)
        requests: dict[str, Request] = {}

        def on_sent(p):
            r = p.get("request", {})
            if not r.get("url", "").startswith(("http://", "https://")):
                return
            requests[p["requestId"]] = Request(url=r["url"], type=(p.get("type") or "").lower())

        def on_response(p):
            req = requests.get(p["requestId"])
            if req is None:
                return
            resp = p.get("response", {})
            req.status = resp.get("status")
            req.mime = resp.get("mimeType", "")
            req.headers = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
            req.protocol = (resp.get("protocol") or "").lower()
            req.type = (p.get("type") or req.type).lower()

        def on_finished(p):
            req = requests.get(p["requestId"])
            if req is not None:
                req.size = int(p.get("encodedDataLength") or 0)

        def on_failed(p):
            req = requests.get(p["requestId"])
            if req is not None:
                req.failed = True

        cdp.on("Network.requestWillBeSent", on_sent)
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.loadingFinished", on_finished)
        cdp.on("Network.loadingFailed", on_failed)
        await cdp.send("Network.enable")
        await cdp.send("Network.emulateNetworkConditions", {
            "offline": False,
            "latency": profile["latency"],
            "downloadThroughput": profile["down_kbps"] * 1024 / 8,
            "uploadThroughput": profile["up_kbps"] * 1024 / 8,
        })
        if profile["cpu"] > 1:
            await cdp.send("Emulation.setCPUThrottlingRate", {"rate": profile["cpu"]})

        await page.goto(url, wait_until="load", timeout=90_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)  # let LCP settle and catch late layout shifts
        metrics = await page.evaluate(PERF_COLLECT_JS)
    finally:
        await context.close()

    reqs = list(requests.values())
    metrics["weight_kb"] = round(sum(r.size for r in reqs) / 1024)
    metrics["requests"] = len(reqs)
    return metrics, reqs


def _flagged(m: dict) -> bool:
    return any(m.get(k) is not None and m[k] > good for k, (good, _) in THRESHOLDS.items())


def _median_metrics(runs: list[dict]) -> dict:
    out = dict(runs[0])
    for key in list(THRESHOLDS) + ["load_ms", "weight_kb", "requests"]:
        vals = [r[key] for r in runs if r.get(key) is not None]
        if vals:
            out[key] = statistics.median(vals)
    return out


async def _run(urls: list[str], config: Config, log: Callable[[str], None]) -> dict[str, SpeedResult]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        from .browser import INSTALL_HINT
        raise RuntimeError(INSTALL_HINT) from exc
    profile = PROFILES[config.speed_profile]
    results: dict[str, SpeedResult] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for i, url in enumerate(urls, 1):
            res = SpeedResult(url=url)
            try:
                runs_wanted = max(1, config.speed_runs)
                while len(res.runs) < runs_wanted:
                    metrics, reqs = await _measure_once(browser, url, config, profile)
                    res.runs.append(metrics)
                    if not res.requests:
                        res.requests = reqs
                    # A page that looks slow gets re-measured: report the median of 3 runs.
                    if len(res.runs) == 1 and runs_wanted < 3 and _flagged(metrics):
                        runs_wanted = 3
                res.metrics = _median_metrics(res.runs)
            except Exception as exc:
                res.error = _short_error(exc)
            results[url] = res
            if i % 5 == 0 or i == len(urls):
                log(f"  speed-tested {i}/{len(urls)} pages")
        await browser.close()
    return results


def run_speed_tests(urls: list[str], config: Config, log: Callable[[str], None]) -> dict[str, SpeedResult]:
    return asyncio.run(_run(urls, config, log))


# ----------------------------------------------------------------------------
# Turning measurements into issues
# ----------------------------------------------------------------------------

def _kb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{round(n / 1024)} KB"


def _secs(ms: float) -> str:
    return f"{ms / 1000:.1f} s"


def _cache_seconds(headers: dict) -> int | None:
    cc = headers.get("cache-control", "").lower()
    if "no-store" in cc or "no-cache" in cc:
        return 0
    m = re.search(r"(?:s-)?max-age=(\d+)", cc)
    if m:
        return int(m.group(1))
    if "expires" in headers:
        return None  # has an Expires date; don't second-guess it
    return 0


def _short_url(u: str) -> str:
    p = urlsplit(u)
    name = p.path.rsplit("/", 1)[-1] or p.path or "/"
    return f"{p.hostname}/…/{name}" if p.path.count("/") > 1 else f"{p.hostname}{p.path}"


def apply_speed_result(page: Page, res: SpeedResult, start_url: str, config: Config, agg: SiteWideIssues) -> None:
    url, title = page.final_url, page.title
    label = PROFILES[config.speed_profile]["label"]
    if res.error:
        page.issues.append(Issue(url, title, "Performance", "Speed test failed", NOTICE, res.error))
        return
    m = res.metrics
    runs = len(res.runs)
    run_note = f" Median of {runs} runs." if runs > 1 else ""
    for key in ("fcp_ms", "lcp_ms", "cls", "tbt_ms", "load_ms", "weight_kb", "requests", "long_tasks"):
        page.metrics[key] = m.get(key)

    def add(issue, severity, details, target=""):
        page.issues.append(Issue(url, title, "Performance", issue, severity, details, target))

    def rate(key):
        v = m.get(key)
        good, poor = THRESHOLDS[key]
        if v is None or v <= good:
            return None
        return WARNING if v > poor else NOTICE

    # --- Core metrics, each with its likely cause ---------------------------
    sev = rate("lcp_ms")
    lcp_el = m.get("lcp_el") or {}
    if sev:
        what = lcp_el.get("selector", "unknown element")
        if lcp_el.get("url"):
            img = next((r for r in res.requests if r.url == lcp_el["url"]), None)
            what += f" (image {lcp_el['url']}" + (f", {_kb(img.size)}" if img and img.size else "") + ")"
        elif lcp_el.get("text"):
            what += f" (text \"{lcp_el['text']}\")"
        add("Slow Largest Contentful Paint (LCP)", sev,
            f"{_secs(m['lcp_ms'])} on {label} (good ≤ 2.5 s, poor > 4 s). The largest element is {what}."
            + run_note, lcp_el.get("url") or lcp_el.get("selector", ""))
    if lcp_el.get("lazy"):
        add("LCP image is lazy-loaded", WARNING,
            "The main (largest) image has loading=\"lazy\", which delays it. Remove lazy-loading from above-the-fold images.",
            lcp_el.get("url", ""))
    sev = rate("fcp_ms")
    if sev:
        add("Slow First Contentful Paint (FCP)", sev,
            f"{_secs(m['fcp_ms'])} before anything appears (good ≤ 1.8 s, poor > 3 s). "
            f"{len(m.get('blocking') or [])} render-blocking file(s)." + run_note)
    sev = rate("cls")
    if sev:
        src = ", ".join(m.get("cls_sources") or []) or "not reported"
        add("Layout shift (CLS)", sev,
            f"CLS {m['cls']} (good ≤ 0.1, poor > 0.25). Elements that moved: {src}. Common causes: images or "
            "embeds without dimensions, late-loading banners/ads, web fonts swapping." + run_note,
            " | ".join(m.get("cls_sources") or []))
    sev = rate("tbt_ms")
    if sev:
        scripts = m.get("scripts") or []
        culprits = ", ".join(f"{_short_url(u)} ({d} ms)" for u, d in scripts) or "not attributed"
        add("High Total Blocking Time (TBT)", sev,
            f"{int(m['tbt_ms'])} ms of main-thread blocking from {m.get('long_tasks', 0)} long task(s) "
            f"(good ≤ 200 ms, poor > 600 ms). Scripts using the most time: {culprits}." + run_note,
            " | ".join(u for u, _ in scripts))

    # --- Page weight breakdown --------------------------------------------
    reqs = [r for r in res.requests if not r.failed]
    total = sum(r.size for r in reqs)
    by_type: dict[str, int] = {}
    for r in reqs:
        by_type[r.type or "other"] = by_type.get(r.type or "other", 0) + r.size
    breakdown = ", ".join(f"{t} {_kb(n)}" for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]) if n > 0)
    if total > PAGE_WEIGHT_NOTICE_KB * 1024:
        add("Large page size", WARNING if total > PAGE_WEIGHT_WARNING_KB * 1024 else NOTICE,
            f"{_kb(total)} transferred: {breakdown}.")
    if len(reqs) > MAX_REQUESTS:
        add("Too many requests", NOTICE, f"{len(reqs)} network requests while loading.")
    js_total = by_type.get("script", 0)
    if js_total > TOTAL_JS_LIMIT_KB * 1024:
        add("Heavy JavaScript", NOTICE, f"{_kb(js_total)} of JavaScript (compressed) on this page.")
    fonts = [r for r in reqs if r.type == "font"]
    if len(fonts) > MAX_FONT_FILES:
        add("Many web font files", NOTICE, f"{len(fonts)} font files; each one delays text rendering.",
            " | ".join(_short_url(f.url) for f in fonts[:6]))

    own = site_domain(urlsplit(start_url).hostname or "")
    third: dict[str, int] = {}
    for r in reqs:
        d = site_domain(urlsplit(r.url).hostname or "")
        if d != own:
            third[d] = third.get(d, 0) + r.size
    third_total = sum(third.values())
    if total and third_total > THIRD_PARTY_MIN_KB * 1024 and third_total / total > THIRD_PARTY_SHARE:
        top = ", ".join(f"{d} {_kb(n)}" for d, n in sorted(third.items(), key=lambda kv: -kv[1])[:5])
        add("Heavy third-party content", NOTICE,
            f"Other companies' scripts/assets are {third_total * 100 // total}% of the page ({_kb(third_total)}): {top}.")

    # --- Site-wide causes (each file reported once) -------------------------
    for b in m.get("blocking") or []:
        if not b["ms"]:
            continue  # blocked by the browser (e.g. mixed content) or served from memory
        agg.add(url, title, "Performance", "Render-blocking resource", NOTICE,
                f"Must load before the page can show anything ({b['ms']} ms on {label}). "
                "Inline critical CSS, or add defer/async to scripts.", b["url"])
    doc = next((r for r in reqs if r.type == "document"), None)
    if doc and doc.protocol and doc.protocol.startswith("http/1"):
        agg.add(url, title, "Performance", "Site not served over HTTP/2", NOTICE,
                f"The page is served over {doc.protocol.upper()}; HTTP/2 or HTTP/3 loads many files faster.",
                urlsplit(url).hostname or "")
    for r in reqs:
        first_party = site_domain(urlsplit(r.url).hostname or "") == own
        if r.status is None or r.status >= 300:
            continue
        enc = r.headers.get("content-encoding", "").lower()
        if (first_party and TEXT_MIME.search(r.mime) and r.size > COMPRESS_MIN_BYTES
                and not any(e in enc for e in ("gzip", "br", "zstd", "deflate"))):
            agg.add(url, title, "Performance", "Text file not compressed", WARNING,
                    f"{_kb(r.size)} {r.mime} sent without gzip/brotli compression (typically 60-80% smaller).", r.url)
        if first_party and r.type in ("script", "stylesheet", "image", "font"):
            secs = _cache_seconds(r.headers)
            if secs is not None and secs < MIN_CACHE_SECONDS:
                cc = r.headers.get("cache-control", "none")
                host = urlsplit(r.url).hostname or ""
                agg.add(url, title, "Performance", "Static files not cached long enough", NOTICE,
                        f"{r.type.capitalize()} files served with Cache-Control: {cc}, so repeat visitors re-download "
                        f"them. Set a long max-age (e.g. 1 year with versioned file names) in the server/CDN config.",
                        f"{host} ({r.type} files, Cache-Control: {cc})", file=r.url)
        if first_party and LEGACY_IMAGE_MIME.search(r.mime) and r.size > LEGACY_IMAGE_MIN_KB * 1024:
            agg.add(url, title, "Images", "Image could use a modern format", NOTICE,
                    f"{_kb(r.size)} {r.mime}; WebP or AVIF is usually 25-50% smaller.", r.url)
        if r.type == "script" and r.size > LARGE_SCRIPT_KB * 1024:
            agg.add(url, title, "Performance", "Large JavaScript file", NOTICE,
                    f"{_kb(r.size)} compressed" + ("" if first_party else " (third-party)") + ".", r.url)
