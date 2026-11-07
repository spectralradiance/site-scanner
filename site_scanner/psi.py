"""Google PageSpeed Insights: Lighthouse run on Google's servers plus real-user (CrUX) field data."""

from __future__ import annotations

import time
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


def order_pages(pages: list[Page], depths: dict[str, int]) -> tuple[list[Page], list[Page]]:
    """Split pages into (one representative per top-level section, everything else), shallowest first.
    The start page always comes first."""
    ordered = sorted(pages, key=lambda p: (depths.get(p.final_url, 10**6), len(p.final_url)))
    reps, rest, sections = [], [], set()
    for p in ordered:
        parts = [s for s in urlsplit(p.final_url).path.split("/") if s]
        section = parts[0] if len(parts) > 1 else ("" if not parts else "(top-level page)")
        if section in sections:
            rest.append(p)
        else:
            sections.add(section)
            reps.append(p)
    return reps, rest


def pick_pages(pages: list[Page], depths: dict[str, int], limit: int) -> list[Page]:
    """The start page plus one page per top-level section (/blog/, /products/...), shallowest first."""
    return order_pages(pages, depths)[0][:limit]


def sample_pages(pages: list[Page], depths: dict[str, int], limit: int | None) -> list[Page]:
    """Pick up to ``limit`` pages (0/None = all) spread across the site: the start page, then one page
    from each top-level section in turn (/details/, /map/, /blog/...), shallowest first within each."""
    if not limit or limit >= len(pages):
        reps, rest = order_pages(pages, depths)
        return reps + rest
    ordered = sorted(pages, key=lambda p: (depths.get(p.final_url, 10**6), len(p.final_url)))
    groups: dict[str, list[Page]] = {}
    for p in ordered:
        parts = [s for s in urlsplit(p.final_url).path.split("/") if s]
        section = parts[0] if len(parts) > 1 else ("" if not parts else "(top-level page)")
        groups.setdefault(section, []).append(p)
    chosen: list[Page] = []
    i = 0
    while len(chosen) < limit:
        added = False
        for members in groups.values():
            if i < len(members):
                chosen.append(members[i])
                added = True
                if len(chosen) >= limit:
                    break
        if not added:
            break
        i += 1
    return chosen


def is_public(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not (host in ("localhost", "127.0.0.1", "::1") or host.endswith((".localhost", ".test", ".local"))
                or host.startswith(("10.", "192.168.")))


def fetch_psi(url: str, strategy: str, api_key: str | None, timeout: float) -> dict:
    """Call the PageSpeed Insights API. Returns {"data": trimmed response} or {"error": message}."""
    if not is_public(url):
        return {"error": "PageSpeed Insights can only test publicly reachable URLs.", "skipped": True}
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if api_key:
        params["key"] = api_key
    err = "no response"
    for attempt in range(2):
        try:
            resp = requests.get(API, params=params, timeout=max(timeout, 90))
        except requests.RequestException as exc:
            err = type(exc).__name__
            continue
        if resp.status_code == 429 and attempt == 0 and api_key:
            time.sleep(10)
            continue
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
            except ValueError:
                err = f"HTTP {resp.status_code}"
            if resp.status_code == 429 and not api_key:
                err += " (rate limited: add a free API key with --psi-key)"
            return {"error": err}
        data = resp.json()
        lh = data.get("lighthouseResult") or {}
        # keep only what _apply needs (full responses are several hundred KB)
        return {"data": {
            "loadingExperience": data.get("loadingExperience"),
            "originLoadingExperience": data.get("originLoadingExperience"),
            "lighthouseResult": {"categories": lh.get("categories"), "audits": lh.get("audits")},
        }}
    return {"error": err}


def apply_psi(page: Page, result: dict, strategy: str) -> None:
    if "data" in result:
        _apply(page, result["data"], strategy)
        return
    issue = "PageSpeed Insights skipped" if result.get("skipped") else "PageSpeed Insights failed"
    page.issues.append(Issue(page.final_url, page.title, "Performance", issue, NOTICE, result.get("error", "")))


def summarize(result: dict) -> str:
    if "data" not in result:
        return "FAILED " + result.get("error", "")
    lh = result["data"].get("lighthouseResult") or {}
    score = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    field = (result["data"].get("loadingExperience") or {}).get("overall_category") or "no field data"
    return f"Lighthouse score {round(score * 100) if score is not None else '-'}, real users: {field}"


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
