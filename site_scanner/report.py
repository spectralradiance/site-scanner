"""CSV output."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .crawler import ScanResult

ISSUE_COLUMNS = ["Page Title", "URL", "Severity", "Category", "Issue", "Details", "Target"]
PAGE_COLUMNS = [
    "URL", "Title", "Status", "Content Type", "Title Length", "Meta Description Length", "H1 Count",
    "Word Count", "Inlinks", "Click Depth", "In Sitemap", "Found Via", "Redirected From", "Issues",
    "Errors", "Warnings", "Response Time (ms)",
    # Filled in by --browser / --speed / --psi
    "TTFB (ms)", "DOM Elements", "FCP (s)", "LCP (s)", "CLS", "TBT (ms)", "Page Weight (KB)", "Requests",
    "PSI Score", "Screenshots",
]


def _fmt(value, scale: float = 1, digits: int = 0):
    if value is None or value == "":
        return ""
    v = value / scale
    return round(v, digits) if digits else int(round(v))


def write_issues_csv(result: ScanResult, path: Path) -> None:
    # utf-8-sig so Excel detects the encoding correctly
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(ISSUE_COLUMNS)
        for i in result.issues:
            w.writerow([i.page_title, i.page_url, i.severity, i.category, i.issue, i.details, i.target])


def write_pages_csv(result: ScanResult, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(PAGE_COLUMNS)
        per_page = Counter()
        per_page_sev = Counter()
        for i in result.issues:
            per_page[i.page_url] += 1
            per_page_sev[(i.page_url, i.severity)] += 1
        for p in sorted(result.pages, key=lambda p: (result.depths.get(p.final_url, 10**6), p.final_url)):
            url = p.final_url
            m = p.metrics
            w.writerow([
                url, p.title, p.status if p.status is not None else p.error, p.content_type,
                len(p.title) if p.is_html else "", len(p.meta_description) if p.is_html else "",
                p.h1_count if p.is_html else "", p.word_count if p.is_html else "",
                result.inlinks.get(url, 0), result.depths.get(url, ""),
                "Yes" if url in result.sitemap_urls else "No", p.found_via,
                p.url if p.url != url else "",
                per_page[url], per_page_sev[(url, "Error")], per_page_sev[(url, "Warning")], p.elapsed_ms,
                _fmt(m.get("ttfb_ms")), _fmt(m.get("dom_size")), _fmt(m.get("fcp_ms"), 1000, 2),
                _fmt(m.get("lcp_ms"), 1000, 2), m.get("cls", ""), _fmt(m.get("tbt_ms")),
                _fmt(m.get("weight_kb")), _fmt(m.get("requests")), m.get("psi_score", ""),
                " | ".join(p.screenshots),
            ])


def summary(result: ScanResult) -> str:
    sev = Counter(i.severity for i in result.issues)
    kinds = Counter((i.severity, i.issue) for i in result.issues)
    lines = [
        f"Pages crawled: {len(result.pages)} ({sum(1 for p in result.pages if p.is_html)} HTML)",
        f"Issues: {len(result.issues)}  (errors {sev.get('Error', 0)}, warnings {sev.get('Warning', 0)}, "
        f"notices {sev.get('Notice', 0)})",
        "Top issues:",
    ]
    order = {"Error": 0, "Warning": 1, "Notice": 2}
    for (severity, issue), n in sorted(kinds.items(), key=lambda kv: (order.get(kv[0][0], 9), -kv[1]))[:15]:
        lines.append(f"  {n:>5}  {severity:<8} {issue}")
    return "\n".join(lines)
