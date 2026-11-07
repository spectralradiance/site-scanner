# Site Scanner

Crawls a website (using its XML sitemap as well as the links on its pages) and writes a CSV of every
issue it finds: broken links, SEO problems, missing featured images, typos, formatting problems,
accessibility failures, orphan (unlinked) pages and, optionally, JavaScript problems, mobile layout
problems and page speed with the causes of slowdowns.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e .
```

For the browser and page-speed checks (`--browser`, `--speed`), also install Playwright, Chromium
and axe-core:

```bash
pip install -e .[browser]
python -m playwright install chromium
```

## Usage

```bash
site-scanner https://example.com                       # HTML checks only (fast)
site-scanner https://example.com --browser             # + checks in a real browser
site-scanner https://example.com --speed               # + page speed (implies --browser)
site-scanner https://example.com --speed --psi 5 --psi-key YOUR_KEY   # + Google PageSpeed Insights
```

Two files are written:

| File | Contents |
|---|---|
| `example.com-issues-<timestamp>.csv` | One row per issue: **Page Title, URL, Severity, Category, Issue, Details, Target** |
| `example.com-pages-<timestamp>.csv` | One row per URL: status, title/description lengths, H1 count, word count, inlinks, click depth, in sitemap?, issue counts, and with `--browser`/`--speed`: server response time, DOM size, FCP, LCP, CLS, TBT, page weight, requests, PSI score, screenshot paths |

### Progress, partial results and resuming

* **Live progress:** every page, link problem, browser check and speed test is printed as it finishes,
  with the elapsed time and an ETA, e.g.
  `[03:12] [speed 4/25 ETA 05:40] 9.8s LCP 3.1s  FCP 1.2s  CLS 0.02  TBT 140ms  1.4 MB, 52 requests  https://...`.
  The same lines go to a log file next to the CSV (`<name>.log`), so you can follow a scan that runs
  somewhere without a live console: `Get-Content reports\site.log -Wait` (PowerShell) or `tail -f`.
* **Partial results:** the CSVs are rewritten with everything found so far every 15 seconds
  (`--report-every`) and after each phase. If a CSV is open in Excel the update is skipped until you close it.
* **Resume:** every finished page, link check, browser check and speed test is saved to
  `.site-scanner/<host>/` as it completes. If a scan is interrupted (Ctrl+C, closed terminal, crash),
  run the same command with `--resume` and only the missing work is done:

  ```bash
  site-scanner https://example.com --speed -o reports/example.csv
  # ...interrupted...
  site-scanner https://example.com --speed -o reports/example.csv --resume
  ```

### What makes a scan slow

* **robots.txt `Crawl-delay`:** some sites ask for up to 60 s between requests. The scanner caps this
  at 1 s (`--max-crawl-delay`) and says so at the start.
* **Big sites behind a small sitemap:** search results and filter pages can lead to hundreds of pages.
  The progress output shows this as it happens; use `--exclude` (e.g. `--exclude "/search/"`) or
  `--max-pages` to rein it in.
* **Browser and speed checks** cost seconds per page, so by default they run on a sample spread across the
  site's sections: 100 pages for `--browser`, 25 for `--speed`. Use `--browser-limit 0` / `--speed-limit 0`
  to cover every page.
* **Slow external sites** fail after 5 s (connect) / 15 s (read), with no long retries. Use `--no-external`
  to skip them entirely.

`Target` holds the specific thing at fault: the broken link URL, the misspelled word, the image, the
CSS selector of the element, the slow script, etc. Rows are sorted Error, then Warning, then Notice.

Problems with shared files or template elements (a render-blocking stylesheet, a JavaScript error in a
site-wide script, low-contrast nav links) are reported **once** with "Affects N page(s)" rather than
repeated on every page.

### Options

| Option | Default | |
|---|---|---|
| `--max-pages N` | 500 | Stop after fetching N pages |
| `--max-depth N` | none | Only follow links N clicks from the start page |
| `--workers N` | 8 | Concurrent requests |
| `--resume` | | Continue the last unfinished scan of this site |
| `--state-dir DIR` | `.site-scanner/<host>` | Where progress is saved for `--resume` |
| `--report-every S` | 15 | Rewrite the CSVs with results so far this often |
| `--delay S` | 0 | Seconds between requests to the site |
| `--max-crawl-delay S` | 1 | Cap on robots.txt `Crawl-delay` |
| `--no-external` | | Don't check links to other websites (much faster) |
| `--exclude REGEX` | | Skip matching URLs, repeatable (e.g. `--exclude "/tag/" --exclude "\?page="`) |
| `--sitemap URL` | auto | Use a specific sitemap (repeatable) |
| `--include-subdomains` | | Treat `blog.example.com` etc. as internal |
| `--ignore-robots` | | Crawl pages robots.txt disallows |
| `--dictionary FILE` | | Extra allowed words, one per line (brand names, jargon) |
| `--check-capitalized` | | Also spell-check capitalised words |
| `--no-spelling` | | Turn off typo detection |
| `--language` | en | Spell-check language (en, es, fr, de, it, pt, nl, ru...) |
| **Browser** | | |
| `--browser` | | Load every page in headless Chromium (see below) |
| `--browser-limit N` | 100 | Browser-check N pages spread across the site's sections (0 = all) |
| `--browser-workers N` | 3 | Pages loaded in parallel |
| `--screenshots DIR` | | Save full-page desktop and mobile screenshots |
| **Speed** | | |
| `--speed` | | Measure page speed and find causes (implies `--browser`) |
| `--speed-profile` | mobile | `mobile` = slow 4G + 4x CPU slowdown (like Lighthouse); `desktop` = fast connection |
| `--speed-runs N` | 1 | Runs per page; the median is reported (3 gives steadier numbers) |
| `--speed-limit N` | 25 | Speed-test N pages: the home page and one per section first (0 = all) |
| `--psi N` | 0 | Run Google PageSpeed Insights on N key pages (home + one per section) |
| `--psi-key KEY` | `$PSI_API_KEY` | PageSpeed Insights API key ([free](https://developers.google.com/speed/docs/insights/v5/get-started)); without one Google rate-limits heavily |
| **Thresholds** | | |
| `--title-max/--title-min` | 60 / 20 | Title length |
| `--description-max/--description-min` | 160 / 50 | Meta description length |
| `--min-words` | 200 | Low word count |
| `--large-image-kb` | 500 | Large image |

Run `site-scanner --help` for the full list.

## What it checks

### Always (from the HTML)

**Links:** broken internal links (4xx/5xx/connection failure), broken external links, broken images,
CSS/JS/favicons, internal links that go through a redirect, malformed hrefs, links with empty or `#` hrefs.
External sites that block bots (401/403/429/999) are reported as *could not be verified* rather than broken.

**SEO:** missing, too long, too short or duplicate titles; missing, too long, too short or duplicate meta
descriptions; missing or multiple H1; missing, multiple or invalid canonical tags; noindex pages.

**Images:** missing featured image (`og:image`), broken or relative featured image, missing alt text,
images over the size threshold.

**Spelling:** possible typos (with a suggested correction and surrounding context) and repeated words
("the the"). Typos in the header, nav and footer are reported once for the whole site. Capitalised
words are skipped (except at the start of a sentence) because they are usually names.
An unrecognised word used on 3 or more pages is almost always a real term (e.g. "wastewater",
"zoonotic"), so it is reported once as *Unrecognised word used on many pages* instead of on every page,
and listed in `<name>-words.txt` next to the report. Delete any real misspellings from that file and pass
it back with `--dictionary <name>-words.txt` on the next scan.

**Formatting:** skipped heading levels, empty headings, very long paragraphs, consecutive `<br>` tags,
empty paragraphs, deprecated tags, missing viewport meta tag, lorem ipsum placeholder text, low word count.

**Accessibility:** links with no accessible text, non-descriptive link text ("click here"), missing
`lang` attribute, missing alt text, and low contrast where colours are set in inline styles.

**Sitemap & discovery:** sitemaps are found via `robots.txt`, then common locations; indexes and `.gz`
files are followed, and every listed URL is crawled.
* **Orphan page:** in the sitemap, but no crawled page links to it.
* **Page not in sitemap:** an indexable page found by crawling that the sitemap doesn't list.
* Sitemap URLs that 404, redirect, are noindexed, are canonicalised elsewhere, are blocked by
  robots.txt, or are on another host.

**Technical:** mixed content (HTTP resources on HTTPS pages), missing robots.txt, crawl limit reached.

### With `--browser` (after JavaScript runs)

Each page is loaded in headless Chromium at desktop size, then resized to phone width.

* **Everything above is re-checked on the rendered page**, and links that JavaScript adds are link-checked too.
* **JavaScript SEO:** title, description or canonical only set (or changed/removed) by JavaScript;
  noindex changed by JavaScript; H1 only added by JavaScript; most of the content only appearing after
  JavaScript; links that only exist after JavaScript. Crawlers that don't run JavaScript miss these.
* **JavaScript errors:** uncaught exceptions and `console.error` messages, with the source location.
* **Failed requests during page load:** scripts, styles, fonts, API calls etc. that 404 or fail,
  including ones requested by JavaScript.
* **Full accessibility audit** with [axe-core](https://github.com/dequelabs/axe-core) (WCAG 2.0-2.2
  A/AA rules: form labels, ARIA, landmarks, keyboard access...). Each row links to the rule's explanation.
* **Colour contrast** using the computed colours of every visible text element (WCAG AA: 4.5:1, or 3:1
  for large text). Text over background images/gradients is skipped.
* **Mobile layout** (375px wide): page scrolls sideways (and which elements cause it), tap targets
  smaller than 24x24px, text smaller than 12px.
* **Images:** much larger than their displayed size, no width/height (causes layout shift), well below
  the first screen but not lazy-loaded.
* **Server response time:** time from the request to the first byte of the page (excluding connection setup).
* **Large DOM** (over 1,500 elements).

### With `--speed` (page speed and causes)

Each page is loaded in a fresh browser (empty cache), one at a time, with throttling that matches
Lighthouse's mobile profile (150 ms latency, 1.6 Mbps, 4x CPU slowdown) or, with
`--speed-profile desktop`, a fast connection.

**Metrics** (Google's thresholds), each reported with its likely cause:

| Metric | Good | Poor | Cause reported |
|---|---|---|---|
| Largest Contentful Paint (LCP) | ≤ 2.5 s | > 4 s | the largest element and, if it's an image, its URL and size; warns if it is lazy-loaded |
| First Contentful Paint (FCP) | ≤ 1.8 s | > 3 s | number of render-blocking files |
| Cumulative Layout Shift (CLS) | ≤ 0.1 | > 0.25 | the elements that moved |
| Total Blocking Time (TBT) | ≤ 200 ms | > 600 ms | the scripts that used the most main-thread time |

**Page-level causes:** large page size (with a breakdown by type), too many requests, heavy JavaScript,
many web font files, heavy third-party content (with the top domains).

**Site-wide causes** (each file reported once): render-blocking CSS/JS, text files sent without
gzip/brotli, static files with short or no cache lifetime (grouped per host and file type), JPEG/PNG/GIF
images that could be WebP/AVIF, large JavaScript files, site not served over HTTP/2.

Lab measurements vary with your machine and connection. Treat them as indicators and compare runs on the
same machine. For real-visitor data use `--psi`.

### With `--psi N` (Google PageSpeed Insights)

Runs Lighthouse on Google's servers for the home page plus one page per site section, and adds:
* **Real-user Core Web Vitals** from the Chrome UX Report (75th percentile LCP, INP, CLS, FCP, TTFB
  over the last 28 days), for the URL or the whole site if the URL has too little traffic.
* The **Lighthouse performance score** and each failing Lighthouse audit with its estimated savings.

Public sites only, about 20-30 seconds per page.

## Limitations

* Without `--browser`, pages are analysed from their HTML source, so content injected by JavaScript isn't seen.
* JavaScript-only links are link-checked, but not crawled further.
* "Unlinked" pages can only be discovered if they appear in the sitemap.
* If the crawl hits `--max-pages`, orphan results may include false positives; the report flags this.
* Spell checking is dictionary-based, not grammar-aware. Treat it as a list of words to review.
* Interaction to Next Paint (INP) needs real user input, so it only comes from `--psi` field data.

## Development

```bash
pip install -e .[dev,browser]
python -m playwright install chromium
pytest
```

The tests serve `tests/site/` (a small site with deliberate problems, plus generated slow responses,
heavy scripts and large images) on a local port and scan it. The browser and speed tests are skipped
if Playwright isn't installed.
