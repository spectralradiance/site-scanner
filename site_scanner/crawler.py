"""Crawl orchestration: sitemap seeding, page crawl, link checking and site-wide checks."""

from __future__ import annotations

import re
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field

from .checks import analyze_page
from .fetcher import Fetcher
from .models import (ERROR, NOTICE, SEVERITY_ORDER, WARNING, Config, Issue, LinkStatus, Page, SiteWideIssues,
                     page_from_dict)
from .progress import Counter
from .sitemap import RobotsInfo, SitemapResult, load_robots, load_sitemaps
from .spelling import SpellCheck
from .urls import is_internal, looks_like_page, normalize_url

UNVERIFIABLE_STATUSES = {401, 403, 405, 406, 429, 999}
FREQUENT_WORD_PAGES = 3   # an unknown word on this many pages is treated as vocabulary


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
    def __init__(self, config: Config, log=_log, state=None, on_checkpoint=None, checkpoint_every: float = 15.0):
        """``state``: optional StateStore for saving/resuming progress.
        ``on_checkpoint(result)``: called with a partial ScanResult every ``checkpoint_every`` seconds
        and after each phase, so reports can be written while the scan is still running."""
        self.config = config
        self.log = log
        self.state = state
        self.on_checkpoint = on_checkpoint
        self.checkpoint_every = checkpoint_every
        self._last_checkpoint = time.monotonic()
        self.inlinks: dict[str, set[str]] = {}
        self.depths: dict[str, int] = {}
        self.phase = "starting"
        self.frequent_words: dict[str, int] = {}
        self.aggs: list[SiteWideIssues] = []   # site-wide issues collected by browser/speed passes
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
        wanted = self.robots.crawl_delay if cfg.respect_robots else None
        if wanted and wanted > self.fetcher.delay:
            self.fetcher.delay = max(self.fetcher.delay, min(wanted, cfg.max_crawl_delay))
            capped = " (capped; use --max-crawl-delay to change)" if wanted > cfg.max_crawl_delay else ""
            self.log(f"robots.txt asks for {wanted:g} s between requests; waiting {self.fetcher.delay:g} s{capped}")
        self.sitemap = load_sitemaps(self.fetcher, self.start, self.robots, cfg.extra_sitemaps)
        if self.sitemap.urls:
            self.log(f"Sitemap: {len(self.sitemap.urls)} URLs from {len(self.sitemap.files)} file(s) "
                     f"(found via {self.sitemap.discovered_from})")
        else:
            self.log("No sitemap URLs found")

        self._phase("crawl", self._crawl)
        if cfg.render:
            # before link checking, so links added by JavaScript are checked too
            self._phase("browser checks", self._browser_checks, optional=True)
        self._phase("link checks", self._check_links)
        self._phase("site-wide checks", self._site_checks)
        if cfg.speed:
            self._phase("speed tests", self._speed_checks, optional=True)
        if cfg.psi_pages:
            self._phase("PageSpeed Insights", self._psi_checks)
        self.phase = "complete"
        return self.result()

    def _phase(self, name: str, fn, optional: bool = False) -> None:
        self.phase = name
        started = time.monotonic()
        self.log(f"--- {name} ---")
        try:
            fn()
        except RuntimeError as exc:
            if not optional:
                raise
            # e.g. Chromium not installed: report it and carry on with the rest of the scan
            self.log(f"WARNING: {name} skipped: {exc}")
            self._site_issue(self.start, "Technical", f"{name.capitalize()} skipped", WARNING, str(exc).splitlines()[0])
            return
        self.log(f"--- {name} done in {time.monotonic() - started:.0f} s ---")
        self.checkpoint(force=True)

    def result(self) -> ScanResult:
        """The scan's results so far (complete once run() has returned)."""
        issues = [i for p in list(self.pages.values()) for i in p.issues] + list(self.site_issues)
        for agg in self.aggs:
            issues.extend(agg.issues())
        issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.page_url, i.category, i.issue, i.target))
        return ScanResult(
            start_url=self.start,
            pages=list(self.pages.values()),
            issues=issues,
            inlinks={k: len(v) for k, v in self.inlinks.items()},
            depths=dict(self.depths),
            sitemap_urls={self._resolve(u) for u in self.sitemap.urls},
            truncated=self.truncated,
        )

    def checkpoint(self, force: bool = False) -> None:
        if self.on_checkpoint is None:
            return
        if not force and time.monotonic() - self._last_checkpoint < self.checkpoint_every:
            return
        self._last_checkpoint = time.monotonic()
        try:
            self.on_checkpoint(self.result())
        except Exception as exc:  # never let report writing stop the scan
            self.log(f"Could not write the in-progress report: {exc}")

    def _save(self, kind: str, key: str, value) -> None:
        if self.state is not None:
            self.state.put(kind, key, value)

    def _saved(self, kind: str, key: str):
        return self.state.get(kind, key) if self.state is not None else None

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

        def handle(url: str, depth: int, via: str, page: Page, saved: bool) -> None:
            page.found_via = via
            self.requested[url] = page
            if not saved:
                self._save("pages", url, page)
            self.fetcher.seed_cache(LinkStatus(url=url, status=page.status, final_url=page.final_url,
                                               error=page.error, content_type=page.content_type))
            if page.final_url != url:
                self.redirects[url] = page.final_url
                seen.add(page.final_url)
            status = page.status if page.status is not None else "ERR"
            note = " (saved)" if saved else f" {page.elapsed_ms / 1000:.1f}s"
            if page.final_url in self.pages:
                self.log(f"[crawl {len(self.pages)} done, {len(queue) + len(futures)} to go] {status}{note} duplicate of "
                         f"{page.final_url} <- {url}")
                return
            self.pages[page.final_url] = page
            if page.error:
                what = page.error
            elif page.is_html:
                what = f"{len(page.issues)} issue(s)"
            else:
                what = page.content_type or "not HTML"
            moved = f" -> {page.final_url}" if page.final_url != url else ""
            self.log(f"[crawl {len(self.pages)} done, {len(queue) + len(futures)} to go] {status}{note} {what}  {url}{moved}")
            if cfg.max_depth is None or depth < cfg.max_depth:
                for link in page.links:
                    if link.kind == "a":
                        enqueue(link.url, depth + 1, "link")
            self.checkpoint()

        pool = ThreadPoolExecutor(max_workers=cfg.workers)
        futures = {}
        try:
            while queue or futures:
                while queue and len(futures) < cfg.workers * 2 and fetched < cfg.max_pages:
                    url, depth, via = queue.popleft()
                    fetched += 1
                    saved = self._saved("pages", url)
                    if saved is not None:
                        handle(url, depth, via, page_from_dict(saved), saved=True)
                        continue
                    futures[pool.submit(self._process, url)] = (url, depth, via)
                if not futures:
                    if queue and fetched < cfg.max_pages:
                        continue
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth, via = futures.pop(fut)
                    handle(url, depth, via, fut.result(), saved=False)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
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
        # Statuses already known: pages fetched by the crawl, and links saved by an earlier run.
        known = 0
        for url in to_check:
            saved = self._saved("links", url)
            if saved is not None:
                self.fetcher.seed_cache(LinkStatus(**saved))
                known += 1
            elif url in self.requested:
                known += 1
        pending = [u for u in to_check if u not in self.requested and self._saved("links", u) is None]
        for url in to_check:
            if url not in pending:
                self.link_status[url] = self.fetcher.check(url)
        referrers: dict[str, int] = defaultdict(int)
        for page in self.pages.values():
            for u in {l.url for l in page.links}:
                referrers[u] += 1
        ext = sum(1 for u in pending if not self._internal(u))
        self.log(f"{len(to_check)} unique link/resource URLs: {known} already known, checking {len(pending)} "
                 f"({len(pending) - ext} on this site, {ext} external)")
        counter = Counter(self.log, "links", len(pending))
        last_line = time.monotonic()
        bad = 0
        pool = ThreadPoolExecutor(max_workers=max(cfg.workers, 8))
        try:
            futs = {pool.submit(self.fetcher.check, u, self._internal(u)): u for u in pending}
            for fut in as_completed(futs):
                st = fut.result()
                self.link_status[st.url] = st
                self._save("links", st.url, st)
                counter.done += 1
                problem = st.status is None or st.status >= 400
                if problem and st.status in UNVERIFIABLE_STATUSES and not self._internal(st.url):
                    counter.tick(f"unverified HTTP {st.status} (site may block bots)  {st.url}", count=False)
                elif problem:
                    bad += 1
                    why = st.error or f"HTTP {st.status}"
                    counter.tick(f"PROBLEM {why}  {st.url}  (linked from {referrers[st.url]} page(s))", count=False)
                elif time.monotonic() - last_line > 5 or counter.done == len(pending):
                    counter.tick(f"{bad} problem(s) so far", count=False)
                else:
                    continue
                last_line = time.monotonic()
                self.checkpoint()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

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
    def _site_checks(self) -> None:
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

        # Unrecognised words used on many pages are almost always real terms (jargon, names), not typos:
        # report each once, and list them so they can be added to a --dictionary file.
        typo_pages: dict[str, list[Page]] = defaultdict(list)
        for page in html_pages:
            for i in page.issues:
                if i.issue == "Possible typo":
                    typo_pages[i.target].append(page)
        self.frequent_words = {w: len(ps) for w, ps in typo_pages.items() if len(ps) >= FREQUENT_WORD_PAGES}
        for word, count in sorted(self.frequent_words.items(), key=lambda kv: -kv[1]):
            for page in typo_pages[word]:
                page.issues = [i for i in page.issues if not (i.issue == "Possible typo" and i.target == word)]
            first = min(typo_pages[word], key=lambda p: (len(p.final_url), p.final_url))
            self._site_issue(first.final_url, "Spelling", "Unrecognised word used on many pages", NOTICE,
                             f"Not in the dictionary but used on {count} pages, so probably a correct term or name. "
                             "If so, add it to a --dictionary file (see the -words.txt file next to this report).",
                             word)

        # Typos in shared template areas (header/nav/footer) are reported once, not per page
        template: dict[str, list] = {}
        for page in sorted(html_pages, key=lambda p: (len(p.final_url), p.final_url)):
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
        self.inlinks, self.depths = inlinks, depths

    # ------------------------------------------------------------------
    def _html_urls(self, limit: int | None) -> list[str]:
        urls = [p.final_url for p in self.pages.values() if p.is_html]
        return urls[:limit] if limit else urls

    def _browser_checks(self) -> None:
        from .browser import apply_browser_result, browser_result_from_dict, run_browser_audit, summarize

        from .psi import sample_pages

        html = [p for p in self.pages.values() if p.is_html]
        urls = [p.final_url for p in sample_pages(html, self.depths, self.config.render_limit)]
        if len(urls) < len(html):
            self.log(f"Browser-checking a sample of {len(urls)} of {len(html)} pages, spread across the site's "
                     f"sections. Use --browser-limit 0 to check every page.")
        agg = SiteWideIssues()
        self.aggs.append(agg)
        counter = Counter(self.log, "browser", len(urls))

        def apply(url, br, seconds, saved=False):
            page = self.pages[url]
            apply_browser_result(page, br, self.config, self.spell, page.headers, agg)
            if not saved:
                self._save("browser", url, br)
            counter.tick(f"{'(saved)' if saved else f'{seconds:.1f}s'} {summarize(br)}  {url}")
            self.checkpoint()

        todo = []
        for url in urls:
            saved = self._saved("browser", url)
            if saved is not None:
                apply(url, browser_result_from_dict(saved), 0, saved=True)
            else:
                todo.append(url)
        if todo:
            self.log(f"Loading {len(todo)} page(s) in headless Chromium, {self.config.browser_workers} at a time")
            run_browser_audit(todo, self.config, self.log, on_result=apply)

    def _speed_checks(self) -> None:
        from .psi import sample_pages
        from .speed import PROFILES, apply_speed_result, run_speed_tests, speed_result_from_dict, summarize

        html = [p for p in self.pages.values() if p.is_html]
        chosen = [p.final_url for p in sample_pages(html, self.depths, self.config.speed_limit)]
        agg = SiteWideIssues()
        self.aggs.append(agg)
        counter = Counter(self.log, "speed", len(chosen))
        if len(chosen) < len(html):
            self.log(f"Speed-testing a sample of {len(chosen)} of {len(html)} pages (the home page and one per "
                     f"section first). Use --speed-limit 0 to test every page.")
        self.log(f"Profile: {PROFILES[self.config.speed_profile]['label']}, {self.config.speed_runs} run(s) per page")

        def apply(url, res, seconds, saved=False):
            apply_speed_result(self.pages[url], res, self.start, self.config, agg)
            if not saved:
                self._save("speed", url, res)
            counter.tick(f"{'(saved)' if saved else f'{seconds:.1f}s'} {summarize(res)}  {url}")
            self.checkpoint()

        todo = []
        for url in chosen:
            saved = self._saved("speed", url)
            if saved is not None:
                apply(url, speed_result_from_dict(saved), 0, saved=True)
            else:
                todo.append(url)
        if todo:
            run_speed_tests(todo, self.config, self.log, on_result=apply)

    def _psi_checks(self) -> None:
        from .psi import apply_psi, fetch_psi, pick_pages, summarize

        pages = pick_pages([p for p in self.pages.values() if p.is_html], self.depths, self.config.psi_pages)
        counter = Counter(self.log, "psi", len(pages))
        self.log(f"Testing {len(pages)} page(s) with Google PageSpeed Insights (about 20-30 s each)")
        for page in pages:
            saved = self._saved("psi", page.final_url)
            started = time.monotonic()
            result = saved if saved is not None else fetch_psi(
                page.final_url, self.config.speed_profile, self.config.psi_key, self.config.timeout)
            if saved is None and "data" in result:
                self._save("psi", page.final_url, result)
            apply_psi(page, result, self.config.speed_profile)
            took = "(saved)" if saved is not None else f"{time.monotonic() - started:.0f}s"
            counter.tick(f"{took} {summarize(result)}  {page.final_url}")
            self.checkpoint()
