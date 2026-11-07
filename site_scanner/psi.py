"""Google PageSpeed Insights: Lighthouse run on Google's servers plus real-user (CrUX) field data."""

from __future__ import annotations

import time
from typing import Callable
from urllib.parse import urlsplit

import requests

from .models import NOTICE, WARNING, Issue, Page

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": ("LCP", lambda v: f"{v / 1000:.1f} s"),
    "INTERACTION_TO_NEXT_PAINT": ("INP", lambda v: f"{v} ms"),
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("CLS", lambda v: f"{v / 100:.2f}"),
    "FIRST_CONTENTFUL_PAINT_MS": ("FCP", lambda v: f"{v / 1000:.1f} s"),
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": ("TTFB", lambda v: f"{v / 1000:.1f} s"),
}


def pick_pages(pages: list[Page], depths: dict[str, int], limit: int) -> list[Page]:
    """The start page plus one page per top-level section (/blog/, /products/...), shallowest first."""
    ordered = sorted(pages, key=lambda p: (depths.get(p.final_url, 10**6), len(p.final_url)))
    chosen, sections = [], set()
    for p in ordered:
        parts = [s for s in urlsplit(p.final_url).path.split("/") if s]
        section = parts[0] if len(parts) > 1 else ("" if not parts else "(top-level page)")
        if section in sections and len(chosen) > 0:
            continue
        sections.add(section)
        chosen.append(p)
        if len(chosen) >= limit:
            break
    return chosen


def is_public(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not (host in ("localhost", "127.0.0.1", "::1") or host.endswith((".localhost", ".test", ".local"))
                or host.startswith(("10.", "192.168.")))


def run_psi(pages: list[Page], strategy: str, api_key: str | None, timeout: float,
            log: Callable[[str], None]) -> None:
    session = requests.Session()
    for i, page in enumerate(pages, 1):
        url, title = page.final_url, page.title
        if not is_public(url):
            page.issues.append(Issue(url, title, "Performance", "PageSpeed Insights skipped", NOTICE,
                                     "PageSpeed Insights can only test publicly reachable URLs."))
            continue
        params = {"url": url, "strategy": strategy, "category": "performance"}
        if api_key:
            params["key"] = api_key
        data = None
        for attempt in range(2):
            try:
                resp = session.get(API, params=params, timeout=max(timeout, 90))
            except requests.RequestException as exc:
                err = f"{type(exc).__name__}"
                continue
            if resp.status_code == 429 and attempt == 0:
                time.sleep(10)
                continue
            if resp.status_code != 200:
                try:
                    err = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
                except ValueError:
                    err = f"HTTP {resp.status_code}"
                if resp.status_code == 429 and not api_key:
                    err += " (rate limited: add a free API key with --psi-key)"
                break
            data = resp.json()
            break
        if data is None:
            page.issues.append(Issue(url, title, "Performance", "PageSpeed Insights failed", NOTICE, err))
            continue
        _apply(page, data, strategy)
        log(f"  PageSpeed Insights {i}/{len(pages)}: {url}")


def _apply(page: Page, data: dict, strategy: str) -> None:
    url, title = page.final_url, page.title

    def add(issue, severity, details, target=""):
        page.issues.append(Issue(url, title, "Performance (PageSpeed Insights)", issue, severity, details, target))

    # Real-user data from the Chrome UX Report
    le = data.get("loadingExperience") or {}
    scope = "this URL"
    if le.get("origin_fallback") or not le.get("metrics"):
        le = data.get("originLoadingExperience") or {}
        scope = "the whole site (not enough data for this URL)"
    for key, (name, fmt) in FIELD_METRICS.items():
        metric = (le.get("metrics") or {}).get(key)
        if not metric:
            continue
        cat = metric.get("category")
        if cat in ("AVERAGE", "SLOW"):
            add(f"Real-user {name} {'is poor' if cat == 'SLOW' else 'needs improvement'}",
                WARNING if cat == "SLOW" else NOTICE,
                f"75th percentile {name} for real Chrome users on {scope}: {fmt(metric.get('percentile', 0))} "
                f"({strategy}, last 28 days).")
    page.metrics["psi_field"] = le.get("overall_category", "")

    # Lab (Lighthouse) results
    lh = data.get("lighthouseResult") or {}
    perf = (lh.get("categories") or {}).get("performance") or {}
    score = perf.get("score")
    if score is not None:
        page.metrics["psi_score"] = round(score * 100)
        if score < 0.9:
            add("Lighthouse performance score", WARNING if score < 0.5 else NOTICE,
                f"{round(score * 100)}/100 ({strategy}). 90+ is good.")
    audits = lh.get("audits") or {}
    for ref in perf.get("auditRefs") or []:
        if ref.get("group") in ("metrics", "hidden") or ref.get("weight", 0) > 0:
            continue  # skip the metric audits themselves
        audit = audits.get(ref.get("id")) or {}
        s = audit.get("score")
        if s is None or s >= 0.9 or audit.get("scoreDisplayMode") in ("notApplicable", "informative", "manual", "error"):
            continue
        details = audit.get("details") or {}
        savings = []
        if details.get("overallSavingsMs"):
            savings.append(f"~{round(details['overallSavingsMs'])} ms")
        if details.get("overallSavingsBytes"):
            savings.append(f"~{round(details['overallSavingsBytes'] / 1024)} KB")
        display = audit.get("displayValue", "")
        add(f"PageSpeed: {audit.get('title', ref.get('id'))}", NOTICE,
            "; ".join(x for x in (display, ("potential savings " + ", ".join(savings)) if savings else "") if x)
            or audit.get("description", "")[:200], ref.get("id", ""))
