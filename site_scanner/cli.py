"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .crawler import Scanner
from .models import Config
from .report import summary, write_issues_csv, write_pages_csv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="site-scanner",
        description="Crawl a website and write a CSV of SEO, link, content, spelling and accessibility issues.",
    )
    p.add_argument("url", help="Start URL, e.g. https://example.com")
    p.add_argument("-o", "--output", help="Issues CSV path (default: <domain>-issues-<timestamp>.csv). "
                                          "A page summary is written alongside as *-pages.csv.")
    g = p.add_argument_group("crawl")
    g.add_argument("--max-pages", type=int, default=500, help="Maximum pages to fetch (default 500)")
    g.add_argument("--max-depth", type=int, default=None, help="Maximum link depth to follow from the start page")
    g.add_argument("--workers", type=int, default=8, help="Concurrent requests (default 8)")
    g.add_argument("--delay", type=float, default=0.0, help="Seconds between requests to the site (default 0)")
    g.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds (default 15)")
    g.add_argument("--user-agent", default=Config.user_agent, help="User-Agent header to send")
    g.add_argument("--sitemap", action="append", default=[], metavar="URL",
                   help="Sitemap URL to use (repeatable). Default: from robots.txt, then /sitemap.xml etc.")
    g.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Skip URLs matching this regex (repeatable), e.g. --exclude '/tag/'")
    g.add_argument("--include-subdomains", action="store_true", help="Treat subdomains as part of the site")
    g.add_argument("--ignore-robots", action="store_true", help="Ignore robots.txt rules")
    g.add_argument("--no-external", action="store_true", help="Don't check links to other websites")
    s = p.add_argument_group("spelling")
    s.add_argument("--no-spelling", action="store_true", help="Disable typo detection")
    s.add_argument("--language", default="en", help="Spell-check language: en, es, fr, de, pt, it, ru, nl... (default en)")
    s.add_argument("--dictionary", metavar="FILE", help="Text file of extra allowed words (one per line)")
    s.add_argument("--check-capitalized", action="store_true",
                   help="Also spell-check capitalised words (more typos found, more false positives from names)")
    c = p.add_argument_group("browser checks (need Playwright: pip install -e .[browser])")
    c.add_argument("--browser", "--render", dest="browser", action="store_true",
                   help="Load pages in headless Chromium: checks after JavaScript runs, JS errors, failed requests, "
                        "axe accessibility audit, colour contrast, mobile layout, image sizing, server response time")
    c.add_argument("--browser-limit", "--render-limit", dest="browser_limit", type=int, default=None,
                   help="Browser-check at most this many pages")
    c.add_argument("--browser-workers", type=int, default=3, help="Pages loaded in parallel (default 3)")
    c.add_argument("--screenshots", metavar="DIR", help="Save desktop and mobile screenshots of each page here")
    sp = p.add_argument_group("page speed")
    sp.add_argument("--speed", action="store_true",
                    help="Measure page speed (LCP, CLS, TBT, FCP, page weight) and find the causes. Implies --browser")
    sp.add_argument("--speed-profile", choices=("mobile", "desktop"), default="mobile",
                    help="mobile = slow 4G + 4x CPU slowdown like Lighthouse (default); desktop = fast connection")
    sp.add_argument("--speed-runs", type=int, default=1,
                    help="Runs per page (default 1; pages that look slow are automatically re-run, median of 3)")
    sp.add_argument("--speed-limit", type=int, default=None, help="Speed-test at most this many pages")
    sp.add_argument("--psi", type=int, default=0, metavar="N",
                    help="Also run Google PageSpeed Insights on N key pages (home + one per section); "
                         "gives real-user Core Web Vitals. Public sites only")
    sp.add_argument("--psi-key", default=os.environ.get("PSI_API_KEY"),
                    help="PageSpeed Insights API key (or set PSI_API_KEY). Optional, but avoids rate limits")
    t = p.add_argument_group("thresholds")
    t.add_argument("--title-max", type=int, default=60)
    t.add_argument("--title-min", type=int, default=20)
    t.add_argument("--description-max", type=int, default=160)
    t.add_argument("--description-min", type=int, default=50)
    t.add_argument("--min-words", type=int, default=200, help="Flag pages with fewer words (default 200)")
    t.add_argument("--large-image-kb", type=int, default=500, help="Flag images larger than this (default 500)")
    return p


def load_words(path: str | None) -> set[str]:
    if not path:
        return set()
    words = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.update(line.split())
    return words


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url = args.url if "://" in args.url else "https://" + args.url

    config = Config(
        start_url=url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        workers=max(1, args.workers),
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
        check_external=not args.no_external,
        respect_robots=not args.ignore_robots,
        include_subdomains=args.include_subdomains,
        extra_sitemaps=args.sitemap,
        exclude_patterns=args.exclude,
        spelling=not args.no_spelling,
        spelling_language=args.language,
        custom_words=load_words(args.dictionary),
        spell_check_capitalized=args.check_capitalized,
        render=args.browser or args.speed,
        render_limit=args.browser_limit,
        browser_workers=max(1, args.browser_workers),
        screenshots_dir=args.screenshots,
        speed=args.speed,
        speed_profile=args.speed_profile,
        speed_runs=max(1, args.speed_runs),
        speed_limit=args.speed_limit,
        psi_pages=max(0, args.psi),
        psi_key=args.psi_key,
        title_max=args.title_max,
        title_min=args.title_min,
        description_max=args.description_max,
        description_min=args.description_min,
        min_word_count=args.min_words,
        large_image_kb=args.large_image_kb,
    )

    if args.output:
        issues_path = Path(args.output)
    else:
        host = (urlsplit(url).hostname or "site").replace(":", "_")
        issues_path = Path(f"{host}-issues-{datetime.now():%Y%m%d-%H%M%S}.csv")
    stem = issues_path.stem
    pages_stem = stem.replace("-issues", "-pages") if "-issues" in stem else stem + "-pages"
    pages_path = issues_path.with_name(pages_stem + ".csv")
    issues_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = Scanner(config).run()
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

    write_issues_csv(result, issues_path)
    write_pages_csv(result, pages_path)
    print(summary(result))
    print(f"\nIssues CSV: {issues_path.resolve()}")
    print(f"Pages CSV:  {pages_path.resolve()}")
    return 0
