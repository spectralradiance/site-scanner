"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .crawler import Scanner
from .models import Config
from .progress import Progress
from .report import summary, write_issues_csv, write_pages_csv
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="site-scanner",
        description="Crawl a website and write a CSV of SEO, link, content, spelling and accessibility issues.",
    )
    p.add_argument("url", help="Start URL, e.g. https://example.com")
    p.add_argument("-o", "--output", help="Issues CSV path (default: <domain>-issues-<timestamp>.csv). "
                                          "A page summary is written alongside as *-pages.csv.")
    r = p.add_argument_group("progress and resuming")
    r.add_argument("--resume", action="store_true",
                   help="Continue the last unfinished scan of this site, skipping work already done")
    r.add_argument("--state-dir", metavar="DIR",
                   help="Where scan progress is saved for --resume (default .site-scanner/<host>)")
    r.add_argument("--report-every", type=float, default=15.0, metavar="SECONDS",
                   help="Rewrite the CSVs with results so far this often (default 15)")
    g = p.add_argument_group("crawl")
    g.add_argument("--max-pages", type=int, default=500, help="Maximum pages to fetch (default 500)")
    g.add_argument("--max-depth", type=int, default=None, help="Maximum link depth to follow from the start page")
    g.add_argument("--workers", type=int, default=8, help="Concurrent requests (default 8)")
    g.add_argument("--delay", type=float, default=0.0, help="Seconds between requests to the site (default 0)")
    g.add_argument("--max-crawl-delay", type=float, default=1.0,
                   help="Cap on the robots.txt Crawl-delay, in seconds (default 1). Some sites ask for 60 s, "
                        "which would make a scan take hours")
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
    c.add_argument("--browser-limit", "--render-limit", dest="browser_limit", type=int, default=100,
                   help="Browser-check this many pages, spread across the site's sections "
                        "(default 100; 0 = every page)")
    c.add_argument("--browser-workers", type=int, default=3, help="Pages loaded in parallel (default 3)")
    c.add_argument("--screenshots", metavar="DIR", help="Save desktop and mobile screenshots of each page here")
    sp = p.add_argument_group("page speed")
    sp.add_argument("--speed", action="store_true",
                    help="Measure page speed (LCP, CLS, TBT, FCP, page weight) and find the causes. Implies --browser")
    sp.add_argument("--speed-profile", choices=("mobile", "desktop"), default="mobile",
                    help="mobile = slow 4G + 4x CPU slowdown like Lighthouse (default); desktop = fast connection")
    sp.add_argument("--speed-runs", type=int, default=1,
                    help="Runs per page; the median is reported (default 1; use 3 for steadier numbers)")
    sp.add_argument("--speed-limit", type=int, default=25,
                    help="Speed-test this many pages: the home page and one per section first, then the shallowest "
                         "others (default 25; 0 = every page)")
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
        line = line.split("#", 1)[0].strip()   # allow "# comments", including after a word
        if line:
            words.update(line.split())
    return words


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:  # show progress immediately even when output is piped (IDEs, CI, Copilot)
            stream.reconfigure(line_buffering=True, errors="replace")
        except (AttributeError, ValueError):
            pass
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
        max_crawl_delay=max(0.0, args.max_crawl_delay),
        psi_pages=max(0, args.psi),
        psi_key=args.psi_key,
        title_max=args.title_max,
        title_min=args.title_min,
        description_max=args.description_max,
        description_min=args.description_min,
        min_word_count=args.min_words,
        large_image_kb=args.large_image_kb,
    )

    host = (urlsplit(url).hostname or "site").replace(":", "_")
    state_root = Path(args.state_dir) if args.state_dir else Path(".site-scanner")
    state_dir = state_root / host
    if args.output:
        issues_path = Path(args.output)
    elif args.resume and StateStore.saved_output(state_dir):
        issues_path = Path(StateStore.saved_output(state_dir))
    else:
        issues_path = Path(f"{host}-issues-{datetime.now():%Y%m%d-%H%M%S}.csv")
    stem = issues_path.stem
    pages_stem = stem.replace("-issues", "-pages") if "-issues" in stem else stem + "-pages"
    pages_path = issues_path.with_name(pages_stem + ".csv")
    log_path = issues_path.with_suffix(".log")
    issues_path.parent.mkdir(parents=True, exist_ok=True)

    log = Progress(log_path)
    log(f"site-scanner {__version__}  |  results: {issues_path.resolve()}")
    log(f"Live log: {log_path.resolve()}  (follow it with: tail -f / Get-Content -Wait)")
    store = StateStore(state_dir, fingerprint(config), args.resume, log, output=str(issues_path))
    warned = {"locked": False}

    def write_reports(result, final: bool = False) -> None:
        for writer, path in ((write_issues_csv, issues_path), (write_pages_csv, pages_path)):
            tmp = path.with_name(path.name + ".tmp")
            writer(result, tmp)
            try:
                os.replace(tmp, path)
            except PermissionError:
                # Excel locks open files on Windows; keep scanning and try again next time.
                tmp.unlink(missing_ok=True)
                if final or not warned["locked"]:
                    log(f"Could not update {path.name} - is it open in Excel? Close it to see updates.")
                    warned["locked"] = True
        if not final:
            log(f"(report updated: {len(result.issues)} issues so far - {issues_path.name})")

    scanner = None
    try:
        scanner = Scanner(config, log=log, state=store, on_checkpoint=write_reports,
                          checkpoint_every=args.report_every)
        result = scanner.run()
    except (ValueError, RuntimeError) as exc:
        log(f"Error: {exc}")
        store.finish(complete=False)
        return 2
    except KeyboardInterrupt:
        log(f"Interrupted during {scanner.phase if scanner else 'start-up'}. Progress is saved.")
        if scanner is not None:
            write_reports(scanner.result(), final=True)
            log(f"Partial results written to {issues_path.resolve()}")
        store.finish(complete=False)
        log(f"Continue with:  site-scanner {args.url} --resume  (plus the same options)")
        log.close()
        sys.stdout.flush()
        # Everything is saved; exit now instead of waiting for in-flight requests to time out.
        os._exit(130)

    store.finish(complete=True)
    write_reports(result, final=True)
    words_path = issues_path.with_name(issues_path.stem.replace("-issues", "") + "-words.txt")
    if scanner.frequent_words:
        lines = ["# Words not in the spell-check dictionary that appear on several pages of the site.",
                 "# Delete any that really are misspelled, then pass this file back with:",
                 f"#   site-scanner {args.url} --dictionary {words_path.name}",
                 "# (count = number of pages using the word)", ""]
        lines += [f"{w}  # {n}" for w, n in sorted(scanner.frequent_words.items(), key=lambda kv: (-kv[1], kv[0]))]
        words_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"{len(scanner.frequent_words)} frequently used unrecognised words listed in {words_path.resolve()}")
    for line in summary(result).splitlines():
        log(line)
    log(f"Issues CSV: {issues_path.resolve()}")
    log(f"Pages CSV:  {pages_path.resolve()}")
    log.close()
    return 0


def fingerprint(config: Config) -> dict:
    """Settings that change results, saved with the state so a resume can warn if they differ."""
    skip = {"workers", "browser_workers", "timeout", "delay", "max_crawl_delay", "psi_key", "screenshots_dir"}
    out = {}
    for k, v in asdict(config).items():
        if k in skip:
            continue
        out[k] = sorted(v) if isinstance(v, set) else v
    return out
