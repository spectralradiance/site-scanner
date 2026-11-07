"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field

ERROR = "Error"
WARNING = "Warning"
NOTICE = "Notice"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, NOTICE: 2}


@dataclass
class Config:
    start_url: str
    max_pages: int = 500
    max_depth: int | None = None
    workers: int = 8
    delay: float = 0.0
    max_crawl_delay: float = 1.0       # cap on robots.txt Crawl-delay
    timeout: float = 15.0
    user_agent: str = "SiteScanner/0.1 (website audit crawler)"
    check_external: bool = True
    respect_robots: bool = True
    include_subdomains: bool = False
    extra_sitemaps: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    # Spelling
    spelling: bool = True
    spelling_language: str = "en"
    custom_words: set[str] = field(default_factory=set)
    spell_check_capitalized: bool = False
    # Browser checks (Playwright): rendered DOM, JS errors, axe, contrast, mobile layout
    render: bool = False
    render_limit: int | None = 100         # pages to browser-check (sampled); 0/None = all
    browser_workers: int = 3
    screenshots_dir: str | None = None
    # Speed (Playwright, throttled)
    speed: bool = False
    speed_profile: str = "mobile"        # "mobile" or "desktop"
    speed_runs: int = 1
    speed_limit: int | None = 25          # pages to speed-test (a sample across sections); 0 = all
    # Google PageSpeed Insights
    psi_pages: int = 0
    psi_key: str | None = None
    # Thresholds
    title_max: int = 60
    title_min: int = 20
    description_max: int = 160
    description_min: int = 50
    min_word_count: int = 200
    long_paragraph_words: int = 200
    large_image_kb: int = 500
    max_page_bytes: int = 10 * 1024 * 1024


@dataclass
class Issue:
    page_url: str
    page_title: str
    category: str
    issue: str
    severity: str
    details: str = ""
    target: str = ""


@dataclass
class Link:
    url: str          # absolute, normalised
    raw: str          # as written in the HTML
    kind: str         # "a", "img", "script", "stylesheet", "og:image"
    text: str = ""    # anchor text / alt text


@dataclass
class Page:
    url: str                      # requested URL (normalised)
    final_url: str = ""           # after redirects (normalised)
    status: int | None = None
    content_type: str = ""
    error: str = ""
    elapsed_ms: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    is_html: bool = False
    title: str = ""
    meta_description: str = ""
    h1_count: int = 0
    word_count: int = 0
    canonical: str = ""
    noindex: bool = False
    og_image: str = ""
    links: list[Link] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    template_typos: dict[str, tuple[str, str]] = field(default_factory=dict)
    found_via: str = "link"       # "start", "link", "sitemap"
    headers: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)       # browser/speed/PSI measurements
    screenshots: list[str] = field(default_factory=list)


def page_from_dict(d: dict) -> Page:
    """Rebuild a Page saved by the state store."""
    d = dict(d)
    d["links"] = [Link(**x) for x in d.get("links", [])]
    d["issues"] = [Issue(**x) for x in d.get("issues", [])]
    d["template_typos"] = {k: tuple(v) for k, v in (d.get("template_typos") or {}).items()}
    known = Page.__dataclass_fields__
    return Page(**{k: v for k, v in d.items() if k in known})


class SiteWideIssues:
    """Collects issues about shared resources (a CSS file, an image...) so each is reported once
    for the whole site, with a count of affected pages, instead of once per page."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def add(self, page_url: str, page_title: str, category: str, issue: str, severity: str,
            details: str, target: str, file: str | None = None) -> None:
        """``file`` groups many files under one row (e.g. every uncached image on a host)."""
        key = (issue, target)
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = {"category": category, "issue": issue, "severity": severity,
                                       "details": details, "pages": {}, "files": {}}
        item["pages"].setdefault(page_url, page_title)
        if file:
            item["files"].setdefault(file, None)

    def issues(self) -> list[Issue]:
        out = []
        for (issue, target), it in self._items.items():
            details = it["details"]
            files = list(it["files"])
            if files:
                details += f" {len(files)} file(s), e.g. " + ", ".join(files[:3]) + ("..." if len(files) > 3 else ".")
            n = len(it["pages"])
            if n > 1:
                details += f" Affects {n} page(s)."
            # Report under the shortest URL (usually the home page) so results don't depend on crawl order.
            page_url = min(it["pages"], key=lambda u: (len(u), u))
            out.append(Issue(page_url, it["pages"][page_url], it["category"], issue, it["severity"], details, target))
        return out


@dataclass
class LinkStatus:
    url: str
    status: int | None = None
    final_url: str = ""
    error: str = ""
    content_type: str = ""
    content_length: int | None = None

    @property
    def redirected(self) -> bool:
        return bool(self.final_url) and self.final_url != self.url
