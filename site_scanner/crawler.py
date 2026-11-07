"""Crawl orchestration: sitemap seeding, page crawl, link checking and site-wide checks."""

from __future__ import annotations

import re
import sys
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .checks import analyze_page
from .fetcher import Fetcher
from .models import ERROR, NOTICE, SEVERITY_ORDER, WARNING, Config, Issue, LinkStatus, Page, SiteWideIssues
from .sitemap import RobotsInfo, SitemapResult, load_robots, load_sitemaps
from .spelling import SpellCheck
from .urls import is_internal, looks_like_page, normalize_url

UNVERIFIABLE_STATUSES = {401, 403, 405, 406, 429, 999}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class ScanResult:
    start_url: str
    pages: list[Page]
    issues: list[Issue]
    inlinks: dict[str, int] = field(default_factory=dict)
    depths: dict[str, int] = field(default_factory=dict)
    sitemap_urls: set[str] = field(default_factory=set)
    truncated: bool = False


class Scanner:
    def __init__(self, config: Config, log=_log):
        self.config = config
        self.log = log
        start = normalize_url(config.start_url)
        if not start:
            raise ValueError(f"Not a valid http(s) URL: {config.start_url}")
        self.start = start
        self.fetcher = Fetcher(config.user_agent, config.timeout, config.delay, pool_size=max(4, config.workers * 2))
        self.spell = (SpellCheck(config.spelling_language, config.custom_words, config.spell_check_capitalized)
                      if config.spelling else None)
        self.excludes = [re.compile(p) for p in config.exclude_patterns]
        self.pages: dict[str, Page] = {}          # keyed by final URL
        self.requested: dict[str, Page] = {}      # keyed by requested URL
        self.redirects: dict[str, str] = {}       # requested URL -> final URL
        self.blocked: set[str] = set()
        self.site_issues: list[Issue] = []
        self.link_status: dict[str, LinkStatus] = {}
        self.robots: RobotsInfo | None = None
        self.sitemap = SitemapResult()
        self.truncated = False

    # ------------------------------------------------------------------
    def _internal(self, url: str) -> bool:
        return is_internal(url, self.start, self.config.include_subdomains)

    def _allowed(self, url: str) -> bool:
        if not self.config.respect_robots or self.robots is None:
            return True
        return self.robots.allowed(url, self.config.user_agent)

    def _excluded(self, url: str) -> bool:
        return any(p.search(url) for p in self.excludes)

    def _resolve(self, url: str) -> str:
        seen = 0
        while url in self.redirects and seen < 10:
            nxt = self.redirects[url]
            if nxt == url:
                break
            url = nxt
            seen += 1
        return url

    def _site_issue(self, url: str, category: str, issue: str, severity: str, details: str = "", target: str = "") -> None:
        page = self.pages.get(self._resolve(url))
        title = page.title if page else ""
        self.site_issues.append(Issue(url, title, category, issue, severity, details, target))

    # ------------------------------------------------------------------
    def run(self) -> ScanResult:
        cfg = self.config
        self.log(f"Scanning {self.start}")
        self.robots = load_robots(self.fetcher, self.start, cfg.user_agent)
        if cfg.respect_robots and self.robots.crawl_delay and self.robots.crawl_delay > self.fetcher.delay:
            self.fetcher.delay = min(self.robots.crawl_delay, 10.0)
            self.log(f"robots.txt requests a crawl delay of {self.robots.crawl_delay}s; using {self.fetcher.delay}s")
        self.sitemap = load_sitemaps(self.fetcher, self.start, self.robots, cfg.extra_sitemaps)
        if self.sitemap.urls:
            self.log(f"Sitemap: {len(self.sitemap.urls)} URLs from {len(self.sitemap.files)} file(s) "
                     f"(found via {self.sitemap.discovered_from})")
        else:
            self.log("No sitemap URLs found")

        self._crawl()
        if cfg.render:
            self._browser_checks()   # before link checking, so JS-added links are checked too
        self._check_links()
        inlinks, depths = self._site_checks()
        if cfg.speed:
            self._speed_checks()
        if cfg.psi_pages:
            self._psi_checks(depths)

        issues = [i for p in self.pages.values() for i in p.issues] + self.site_issues
        issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.page_url, i.category, i.issue, i.target))
        return ScanResult(
            start_url=self.start,
            pages=list(self.pages.values()),
            issues=issues,
            inlinks={k: len(v) for k, v in inlinks.items()},
            depths=depths,
            sitemap_urls={self._resolve(u) for u in self.sitemap.urls},
            truncated=self.truncated,
        )

    # ------------------------------------------------------------------
    def _process(self, url: str) -> Page:
        res = self.fetcher.get(url, max_bytes=self.config.max_page_bytes)
        page = Page(
            url=url,
            final_url=res.final_url or url,
            status=res.status,
            content_type=res.content_type.split(";")[0].strip(),
            error=res.error,
            elapsed_ms=res.elapsed_ms,
            redirect_chain=res.redirect_chain,
            headers=res.headers,
        )
        if res.status and 200 <= res.status < 300 and res.is_html and self._internal(page.final_url):
            page.is_html = True
            try:
                analyze_page(page, res, self.config, self.spell)
            except Exception as exc:  # keep crawling even if one page breaks the parser
                page.issues.append(Issue(page.final_url, page.title, "Technical", "Page could not be analysed",
                                         ERROR, f"{type(exc).__name__}: {exc}"))
        return page

    def _crawl(self) -> None:
        cfg = self.config
        queue: deque[tuple[str, int, str]] = deque()
        seen: set[str] = set()

        def enqueue(url: str, depth: int, via: str) -> None:
            if url in seen:
                return
            seen.add(url)
            if not self._internal(url) or not looks_like_page(url) or self._excluded(url):
                return
            if not self._allowed(url):
                self.blocked.add(url)
                return
            queue.append((url, depth, via))

        enqueue(self.start, 0, "start")
        for u in self.sitemap.urls:
            enqueue(u, 0, "sitemap")

        fetched = 0
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            futures = {}
            while queue or futures:
                while queue and len(futures) < cfg.workers * 2 and fetched < cfg.max_pages:
                    url, depth, via = queue.popleft()
                    futures[pool.submit(self._process, url)] = (url, depth, via)
                    fetched += 1
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth, via = futures.pop(fut)
                    page = fut.result()
                    page.found_via = via
                    self.requested[url] = page
                    self.fetcher.seed_cache(LinkStatus(url=url, status=page.status, final_url=page.final_url,
                                                       error=page.error, content_type=page.content_type))
                    if page.final_url != url:
                        self.redirects[url] = page.final_url
                        seen.add(page.final_url)
                    if page.final_url in self.pages:
                        continue
                    self.pages[page.final_url] = page
                    if len(self.pages) % 25 == 0:
                        self.log(f"  crawled {len(self.pages)} pages ({len(queue)} queued)")
                    if cfg.max_depth is None or depth < cfg.max_depth:
                        for link in page.links:
                            if link.kind == "a":
                                enqueue(link.url, depth + 1, "link")
        self.truncated = bool(queue)
        self.log(f"Crawled {len(self.pages)} pages" + (" (page limit reached)" if self.truncated else ""))

    # ------------------------------------------------------------------
    def _check_links(self) -> None:
        cfg = self.config
        targets: set[str] = set()
        for page in self.pages.values():
            for link in page.links:
                targets.add(link.url)
        to_check = []
        for url in targets:
            internal = self._internal(url)
            if internal and not self._allowed(url):
                continue
            if not internal and not cfg.check_external:
                continue
            to_check.append(url)
        self.log(f"Checking {len(to_check)} unique link/resource URLs")
        with ThreadPoolExecutor(max_workers=max(cfg.workers, 8)) as pool:
            results = pool.map(lambda u: self.fetcher.check(u, polite=self._internal(u)), to_check)
            for i, st in enumerate(results, 1):
                self.link_status[st.url] = st
                if i % 200 == 0:
                    self.log(f"  checked {i}/{len(to_check)}")

        kind_names = {"a": "link", "img": "image", "resource": "resource", "og:image": "featured image"}
        for page in self.pages.values():
            done: set[tuple[str, str]] = set()
            for link in page.links:
                key = (link.url, link.kind)
                if key in done:
                    continue
                done.add(key)
                st = self.link_status.get(link.url)
                if st is None:
                    continue
                internal = self._internal(link.url)
                noun = kind_names.get(link.kind, "link")
                category = "Images" if link.kind in ("img", "og:image") else "Links"
                where = f" Link text: \"{link.text}\"." if link.kind == "a" and link.text else ""
                if st.status is None:
                    page.issues.append(Issue(page.final_url, page.title, category,
                                             f"Broken {noun}" + ("" if internal else " (external)"), ERROR,
                                             f"Request failed: {st.error}.{where}", link.url))
                elif st.status >= 400:
                    if not internal and st.status in UNVERIFIABLE_STATUSES:
                        page.issues.append(Issue(page.final_url, page.title, category,
                                                 f"External {noun} could not be verified", NOTICE,
                                                 f"HTTP {st.status} - the site may block automated checks.{where}",
                                                 link.url))
                    else:
                        page.issues.append(Issue(page.final_url, page.title, category,
                                                 f"Broken {noun}" + ("" if internal else " (external)"),
                                                 ERROR, f"HTTP {st.status}.{where}", link.url))
                else:
                    if internal and link.kind == "a" and st.redirected:
                        page.issues.append(Issue(page.final_url, page.title, "Links", "Internal link redirects",
                                                 NOTICE, f"Redirects to {st.final_url}; link to it directly.{where}",
                                                 link.url))
                    if (link.kind in ("img", "og:image") and st.content_length
                            and st.content_length > cfg.large_image_kb * 1024):
                        page.issues.append(Issue(page.final_url, page.title, "Images", "Large image file", NOTICE,
                                                 f"{st.content_length // 1024} KB (threshold {cfg.large_image_kb} KB).",
                                                 link.url))
                    if link.kind == "og:image" and st.content_type and not st.content_type.startswith("image/"):
                        page.issues.append(Issue(page.final_url, page.title, "Images",
                                                 "Featured image is not an image", WARNING,
                                                 f"og:image returns Content-Type {st.content_type}.", link.url))

    # ------------------------------------------------------------------
    def _site_checks(self) -> tuple[dict[str, set[str]], dict[str, int]]:
        cfg = self.config
        html_pages = [p for p in self.pages.values() if p.is_html]

        # Link graph (resolved through redirects)
        inlinks: dict[str, set[str]] = defaultdict(set)
        graph: dict[str, set[str]] = defaultdict(set)
        for page in html_pages:
            for link in page.links:
                if link.kind != "a" or not self._internal(link.url):
                    continue
                target = self._resolve(link.url)
                if target != page.final_url:
                    inlinks[target].add(page.final_url)
                    graph[page.final_url].add(target)

        start = self._resolve(self.start)
        depths: dict[str, int] = {start: 0}
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt in graph.get(cur, ()):
                if nxt not in depths:
                    depths[nxt] = depths[cur] + 1
                    q.append(nxt)

        # robots.txt / sitemap presence
        if self.robots and not self.robots.found:
            status = self.robots.status
            self._site_issue(self.robots.url, "Technical", "robots.txt not found", NOTICE,
                             f"HTTP {status}" if status else "robots.txt could not be fetched.")
        if not self.sitemap.urls and not self.sitemap.errors:
            self._site_issue(self.start, "Sitemap", "No XML sitemap found", WARNING,
                             "Checked robots.txt and common locations (/sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml).")
        for sm_url, problem in self.sitemap.errors:
            self._site_issue(sm_url, "Sitemap", "Sitemap problem", ERROR, problem, sm_url)

        # Sitemap URL health + orphans
        sitemap_resolved: set[str] = set()
        for sm_page_url, source in self.sitemap.urls.items():
            src = f"Listed in {source}."
            if not self._internal(sm_page_url):
                self._site_issue(sm_page_url, "Sitemap", "Sitemap URL on a different host", WARNING, src, sm_page_url)
                continue
            if sm_page_url in self.blocked:
                self._site_issue(sm_page_url, "Sitemap", "Sitemap URL blocked by robots.txt", WARNING, src, sm_page_url)
                continue
            fetched = self.requested.get(sm_page_url)
            if fetched is None:
                continue  # not crawled (page limit / exclusions)
            final = self._resolve(sm_page_url)
            sitemap_resolved.add(final)
            if fetched.status is None:
                self._site_issue(sm_page_url, "Sitemap", "Sitemap URL unreachable", ERROR,
                                 f"{fetched.error}. {src}", sm_page_url)
                continue
            if fetched.status >= 400:
                self._site_issue(sm_page_url, "Sitemap", "Sitemap URL returns an error", ERROR,
                                 f"HTTP {fetched.status}. {src}", sm_page_url)
                continue
            if final != sm_page_url:
                self._site_issue(sm_page_url, "Sitemap", "Sitemap URL redirects", WARNING,
                                 f"Redirects to {final}; list the final URL instead. {src}", final)
            page = self.pages.get(final)
            if page is None or not page.is_html:
                continue
            if page.noindex:
                self._site_issue(final, "Sitemap", "Noindex page in sitemap", WARNING,
                                 "The page asks not to be indexed but is listed in the sitemap.", sm_page_url)
            if page.canonical and page.canonical != final:
                self._site_issue(final, "Sitemap", "Sitemap URL is canonicalised elsewhere", NOTICE,
                                 f"Canonical points to {page.canonical}.", page.canonical)
            if final != start and not inlinks.get(final):
                self._site_issue(final, "Links", "Orphan page (in sitemap but not linked)", WARNING,
                                 "No crawled page links to this URL; visitors and search engines can only reach it "
                                 "via the sitemap." + (" Crawl limit was reached, so this may be a false positive."
                                                       if self.truncated else ""), sm_page_url)

        if self.sitemap.urls:
            for page in html_pages:
                if (page.final_url not in sitemap_resolved and not page.noindex
                        and (not page.canonical or page.canonical == page.final_url)):
                    self._site_issue(page.final_url, "Sitemap", "Page not in sitemap", NOTICE,
                                     "Indexable page found by crawling but missing from the XML sitemap.")

        # Duplicates
        for attr, label in (("title", "Duplicate page title"), ("meta_description", "Duplicate meta description")):
            groups: dict[str, list[Page]] = defaultdict(list)
            for page in html_pages:
                value = getattr(page, attr).strip().lower()
                if value and not page.noindex:
                    groups[value].append(page)
            for value, group in groups.items():
                if len(group) < 2:
                    continue
                for page in group:
                    others = [p.final_url for p in group if p is not page]
                    self._site_issue(page.final_url, "SEO", label, WARNING,
                                     f"Shared with {len(others)} other page(s).",
                                     " | ".join(others[:5]) + (" ..." if len(others) > 5 else ""))

        # Typos in shared template areas (header/nav/footer) are reported once, not per page
        template: dict[str, list] = {}
        for page in html_pages:
            for word, (suggestion, ctx) in page.template_typos.items():
                entry = template.setdefault(word, [page.final_url, suggestion, ctx, 0])
                entry[3] += 1
        for word, (first_url, suggestion, ctx, count) in sorted(template.items()):
            hint = f"Did you mean \"{suggestion}\"? " if suggestion else ""
            self._site_issue(first_url, "Spelling", "Possible typo (header/nav/footer)", NOTICE,
                             f"{hint}Appears on {count} page(s). Context: {ctx}", word)

        if self.truncated:
            self._site_issue(self.start, "Technical", "Crawl limit reached", NOTICE,
                             f"Stopped after {cfg.max_pages} pages; raise --max-pages for a complete scan.")
        return inlinks, depths

    # ------------------------------------------------------------------
    def _html_urls(self, limit: int | None) -> list[str]:
        urls = [p.final_url for p in self.pages.values() if p.is_html]
        return urls[:limit] if limit else urls

    def _browser_checks(self) -> None:
        from .browser import apply_browser_result, run_browser_audit

        urls = self._html_urls(self.config.render_limit)
        self.log(f"Browser checks: loading {len(urls)} pages in headless Chromium")
        results = run_browser_audit(urls, self.config, self.log)
        agg = SiteWideIssues()
        for url, br in results.items():
            page = self.pages[url]
            apply_browser_result(page, br, self.config, self.spell, page.headers, agg)
        self.site_issues.extend(agg.issues())

    def _speed_checks(self) -> None:
        from .speed import apply_speed_result, run_speed_tests

        urls = self._html_urls(self.config.speed_limit)
        self.log(f"Speed tests: measuring {len(urls)} pages one at a time ({self.config.speed_profile} profile)")
        results = run_speed_tests(urls, self.config, self.log)
        agg = SiteWideIssues()
        for url, res in results.items():
            apply_speed_result(self.pages[url], res, self.start, self.config, agg)
        self.site_issues.extend(agg.issues())

    def _psi_checks(self, depths: dict[str, int]) -> None:
        from .psi import pick_pages, run_psi

        pages = pick_pages([p for p in self.pages.values() if p.is_html], depths, self.config.psi_pages)
        self.log(f"PageSpeed Insights: testing {len(pages)} page(s) (about 20-30 s each)")
        run_psi(pages, self.config.speed_profile, self.config.psi_key, self.config.timeout, self.log)
