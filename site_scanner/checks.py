"""Per-page analysis: extracts page data and links, and records on-page issues."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction
from bs4.element import NavigableString, Tag

from .contrast import static_contrast
from .fetcher import FetchResult
from .models import ERROR, NOTICE, WARNING, Config, Issue, Link, Page
from .spelling import SpellCheck
from .urls import is_placeholder_href, is_skippable_href, normalize_url

try:  # lxml is faster, but optional
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

SKIP_TEXT_TAGS = {
    "script", "style", "noscript", "template", "svg", "code", "pre", "kbd", "samp", "var",
    "textarea", "iframe", "object", "math", "head", "title", "select", "option",
}
TEMPLATE_TAGS = {"header", "nav", "footer", "aside"}
TEMPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
GENERIC_LINK_TEXT = {
    "click here", "here", "click", "read more", "more", "learn more", "this link", "link",
    "this page", "go", "details", "more info", "continue", "find out more",
}
DEPRECATED_TAGS = {"font", "center", "marquee", "blink", "big", "strike", "tt", "frame", "frameset"}
REPEATED_WORD_RE = re.compile(r"\b([A-Za-z]{2,})\s+\1\b", re.I)
ALLOWED_REPEATS = {"that", "had", "is", "bye", "ha", "no", "very", "so", "knock", "tut", "chop", "yada", "blah"}
WORD_RE = re.compile(r"[A-Za-z0-9À-ɏ]+(?:['’-][A-Za-z0-9À-ɏ]+)*")
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_NON_TEXT = (Comment, Doctype, CData, ProcessingInstruction, Declaration)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _shorten(text: str, n: int = 80) -> str:
    text = _clean(text)
    return text if len(text) <= n else text[: n - 3] + "..."


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    style = tag.get("style", "")
    if style:
        s = style.replace(" ", "").lower()
        if "display:none" in s or "visibility:hidden" in s:
            return True
    return False


def text_nodes(soup: BeautifulSoup) -> list[tuple[str, bool]]:
    """Visible text strings in <body>, each flagged True if it belongs to the site template
    (header/nav/footer/aside, or anything outside <main> when the page has one)."""
    body = soup.body or soup
    main = body.find("main") or body.find(attrs={"role": "main"})
    out = []
    for s in body.find_all(string=True):
        if isinstance(s, _NON_TEXT):
            continue
        text = _clean(s)
        if not text:
            continue
        skip = template = in_main = False
        for parent in s.parents:
            if parent.name in SKIP_TEXT_TAGS or (isinstance(parent, Tag) and parent is not soup and _is_hidden(parent)):
                skip = True
                break
            if parent.name in TEMPLATE_TAGS or parent.get("role") in TEMPLATE_ROLES:
                template = True
            if main is not None and parent is main:
                in_main = True
        if skip:
            continue
        if main is not None:
            template = not in_main
        out.append((text, template))
    return out


def analyze_page(page: Page, res: FetchResult, config: Config, spell: SpellCheck | None) -> None:
    soup = BeautifulSoup(res.content, PARSER, from_encoding=res.encoding)
    url = page.final_url
    head = soup.head or soup

    # ---- title ----------------------------------------------------------
    titles = [t for t in soup.find_all("title") if not t.find_parent("svg")]
    title = _clean(titles[0].get_text()) if titles else ""
    page.title = title
    issues: list[Issue] = []

    def add(category: str, issue: str, severity: str, details: str = "", target: str = "") -> None:
        issues.append(Issue(url, title, category, issue, severity, details, target))

    if not title:
        add("SEO", "Missing page title", ERROR, "No <title> tag, or the title is empty.")
    else:
        if len(title) > config.title_max:
            add("SEO", "Title too long", WARNING,
                f"{len(title)} characters (recommended max {config.title_max}); may be truncated in search results.")
        elif len(title) < config.title_min:
            add("SEO", "Title too short", NOTICE, f"{len(title)} characters (recommended min {config.title_min}).")
    if len(titles) > 1:
        add("SEO", "Multiple title tags", WARNING, f"{len(titles)} <title> tags found.")

    # ---- meta description -------------------------------------------------
    descs = soup.find_all("meta", attrs={"name": re.compile(r"^\s*description\s*$", re.I)})
    desc = _clean(descs[0].get("content")) if descs else ""
    page.meta_description = desc
    if not desc:
        add("SEO", "Missing meta description", WARNING, "No <meta name=\"description\"> or it is empty.")
    elif len(desc) > config.description_max:
        add("SEO", "Meta description too long", WARNING,
            f"{len(desc)} characters (recommended max {config.description_max}).")
    elif len(desc) < config.description_min:
        add("SEO", "Meta description too short", NOTICE,
            f"{len(desc)} characters (recommended min {config.description_min}).")
    if len(descs) > 1:
        add("SEO", "Multiple meta descriptions", WARNING, f"{len(descs)} description meta tags found.")

    # ---- robots / canonical ---------------------------------------------
    robots_meta = " ".join(
        (m.get("content") or "") for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)})
    ).lower()
    x_robots = str(res.headers.get("X-Robots-Tag", "")).lower()
    page.noindex = "noindex" in robots_meta or "noindex" in x_robots
    if page.noindex:
        add("SEO", "Page is set to noindex", NOTICE, "Search engines are asked not to index this page.")

    canonicals = soup.find_all("link", rel=lambda v: v and "canonical" in [x.lower() for x in (v if isinstance(v, list) else [v])])
    if not canonicals:
        add("SEO", "Missing canonical tag", NOTICE, "No <link rel=\"canonical\"> found.")
    else:
        canon = normalize_url(canonicals[0].get("href", ""), url) or ""
        page.canonical = canon
        if len(canonicals) > 1:
            add("SEO", "Multiple canonical tags", WARNING, f"{len(canonicals)} canonical tags found.")
        if canon and canon != url:
            add("SEO", "Canonical points to a different URL", NOTICE, f"Canonical is {canon}", canon)
        if not canon:
            add("SEO", "Invalid canonical URL", WARNING, f"Canonical href: {canonicals[0].get('href', '')!r}")

    # ---- base URL for resolving relative links ----------------------------
    base_url = url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = normalize_url(base_tag["href"], url) or url

    # ---- featured image (Open Graph) ------------------------------------
    og = soup.find("meta", attrs={"property": re.compile(r"^og:image(:url)?$", re.I)}) or \
        soup.find("meta", attrs={"name": re.compile(r"^og:image$", re.I)})
    tw = soup.find("meta", attrs={"name": re.compile(r"^twitter:image(:src)?$", re.I)})
    og_raw = (og.get("content") or "").strip() if og else ""
    if not og_raw:
        extra = " A twitter:image tag is present, but og:image is what most platforms use." if tw else ""
        add("Images", "Missing featured image (og:image)", WARNING,
            "No og:image meta tag; shared links will have no preview image." + extra)
    else:
        og_url = normalize_url(og_raw, base_url)
        if og_url:
            page.og_image = og_url
            page.links.append(Link(og_url, og_raw, "og:image"))
            if not og_raw.lower().startswith(("http://", "https://", "//")):
                add("Images", "Featured image URL is relative", NOTICE,
                    "og:image should be an absolute URL.", og_raw)
        else:
            add("Images", "Invalid featured image URL", WARNING, f"og:image content: {og_raw!r}", og_raw)

    # ---- headings -------------------------------------------------------
    headings = soup.find_all(HEADINGS)
    h1s = [h for h in headings if h.name == "h1"]
    page.h1_count = len(h1s)
    if not h1s:
        add("SEO", "Missing H1 heading", WARNING, "The page has no <h1>.")
    elif len(h1s) > 1:
        add("SEO", "Multiple H1 headings", NOTICE, f"{len(h1s)} <h1> tags found.",
            " | ".join(_shorten(h.get_text(), 50) for h in h1s[:5]))
    prev_level = None
    for h in headings:
        level = int(h.name[1])
        text = _clean(h.get_text())
        if not text and not h.find("img", alt=lambda a: a and a.strip()):
            add("Formatting", "Empty heading", WARNING, f"<{h.name}> has no text.")
        if prev_level is not None and level > prev_level + 1:
            add("Formatting", "Skipped heading level", NOTICE,
                f"H{prev_level} is followed by H{level} (expected H{prev_level + 1} or higher).", _shorten(text, 60))
        prev_level = level

    # ---- links ----------------------------------------------------------
    placeholder_links = []
    for a in soup.find_all("a"):
        if not a.has_attr("href"):
            continue
        href = a.get("href", "")
        text = _clean(a.get_text(" "))
        if not text:
            img = a.find("img", alt=True)
            text = _clean(a.get("aria-label") or a.get("title") or (img.get("alt") if img else ""))
            if not text and not a.find("svg", attrs={"aria-label": True}):
                add("Accessibility", "Link has no text", WARNING,
                    "Screen readers can't describe this link (no text, aria-label, title or image alt).",
                    href.strip()[:200])
        elif text.lower().strip(" .!>»→") in GENERIC_LINK_TEXT:
            add("Accessibility", "Non-descriptive link text", NOTICE,
                f"Link text \"{text}\" doesn't describe the destination.", href.strip()[:200])
        if is_placeholder_href(href):
            placeholder_links.append(text or "(no text)")
            continue
        if is_skippable_href(href) or href.strip().startswith("#"):
            continue
        link_url = normalize_url(href, base_url)
        if link_url is None:
            add("Links", "Malformed link URL", WARNING, "The href could not be parsed as a URL.", href.strip()[:200])
            continue
        page.links.append(Link(link_url, href, "a", text))
    if placeholder_links:
        add("Links", "Links with empty or '#' href", NOTICE,
            f"{len(placeholder_links)} link(s) point nowhere: " + ", ".join(_shorten(t, 30) for t in placeholder_links[:10]))

    # ---- images ---------------------------------------------------------
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "").strip()
        if not src:
            add("Images", "Image has no src", NOTICE, "An <img> tag has no src attribute.", str(img)[:120])
            continue
        if not img.has_attr("alt"):
            add("Accessibility", "Image missing alt text", WARNING,
                "Add alt text (or alt=\"\" if purely decorative).", src[:300])
        if src.lower().startswith("data:"):
            continue
        img_url = normalize_url(src, base_url)
        if img_url:
            page.links.append(Link(img_url, src, "img", _clean(img.get("alt"))))

    # ---- other resources (CSS/JS/favicons) -------------------------------
    for tag, attr in (("script", "src"), ("link", "href")):
        for el in soup.find_all(tag, attrs={attr: True}):
            if tag == "link":
                rels = [r.lower() for r in (el.get("rel") or [])]
                if not any(r in ("stylesheet", "icon", "apple-touch-icon", "shortcut") for r in rels):
                    continue
            res_url = normalize_url(el[attr], base_url)
            if res_url:
                page.links.append(Link(res_url, el[attr], "resource", tag))

    # ---- mixed content --------------------------------------------------
    if url.startswith("https://"):
        mixed = {l.url for l in page.links if l.kind in ("img", "resource") and l.url.startswith("http://")}
        for el in soup.find_all(["iframe", "video", "audio", "source"], src=True):
            if el["src"].strip().lower().startswith("http://"):
                mixed.add(el["src"].strip())
        for m in sorted(mixed):
            add("Technical", "Mixed content (HTTP resource on HTTPS page)", WARNING,
                "Browsers may block or warn about insecure resources.", m)

    # ---- document-level formatting -----------------------------------------
    html_tag = soup.find("html")
    if html_tag is not None and not (html_tag.get("lang") or "").strip():
        add("Accessibility", "Missing lang attribute", NOTICE, "<html> has no lang attribute.")
    if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}):
        add("Formatting", "Missing viewport meta tag", WARNING, "Page may not display correctly on mobile devices.")
    deprecated = sorted({t.name for t in soup.find_all(DEPRECATED_TAGS)})
    if deprecated:
        add("Formatting", "Deprecated HTML tags", NOTICE, "Uses " + ", ".join(f"<{t}>" for t in deprecated))
    br_runs = 0
    for br in soup.find_all("br"):
        nxt = br.next_sibling
        while isinstance(nxt, NavigableString) and not nxt.strip():
            nxt = nxt.next_sibling
        if isinstance(nxt, Tag) and nxt.name == "br":
            prev = br.previous_sibling
            while isinstance(prev, NavigableString) and not prev.strip():
                prev = prev.previous_sibling
            if not (isinstance(prev, Tag) and prev.name == "br"):
                br_runs += 1
    if br_runs:
        add("Formatting", "Line breaks used for spacing", NOTICE,
            f"{br_runs} place(s) with consecutive <br> tags; use paragraphs or CSS margins instead.")
    empty_paras = sum(1 for p in soup.find_all("p") if not _clean(p.get_text()) and not p.find(["img", "iframe", "video", "input", "button"]))
    if empty_paras >= 2:
        add("Formatting", "Empty paragraphs", NOTICE, f"{empty_paras} empty <p> tags (often used for spacing).")

    # ---- text content ----------------------------------------------------
    nodes = text_nodes(soup)
    all_text = [t for t, _ in nodes]
    page.word_count = sum(len(WORD_RE.findall(t)) for t in all_text)
    content_text = [t for t, tpl in nodes if not tpl]
    if page.word_count < config.min_word_count and not page.noindex:
        add("Content", "Low word count", NOTICE,
            f"{page.word_count} words of visible text (threshold {config.min_word_count}).")
    joined = " ".join(all_text)
    if re.search(r"\blorem ipsum\b", joined, re.I):
        add("Content", "Placeholder text (lorem ipsum)", ERROR, "The page contains 'lorem ipsum' placeholder text.")
    for p in soup.find_all("p"):
        words = len(WORD_RE.findall(p.get_text(" ")))
        if words > config.long_paragraph_words:
            add("Formatting", "Very long paragraph", NOTICE,
                f"{words} words in one paragraph; consider breaking it up.", _shorten(p.get_text(" "), 60))
    seen_repeats = set()
    for t in all_text:
        for m in REPEATED_WORD_RE.finditer(t):
            w = m.group(1).lower()
            if w in ALLOWED_REPEATS or w in seen_repeats or w.isdigit():
                continue
            seen_repeats.add(w)
            s = max(0, m.start() - 30)
            add("Spelling", "Repeated word", WARNING, f"\"{m.group(0)}\" in: ...{t[s:m.end() + 30]}...", m.group(0))

    if spell is not None:
        for word, (suggestion, ctx) in spell.find_typos(content_text).items():
            hint = f"Did you mean \"{suggestion}\"? " if suggestion else ""
            add("Spelling", "Possible typo", NOTICE, f"{hint}Context: {ctx}", word)
        page.template_typos = spell.find_typos(t for t, tpl in nodes if tpl)

    # ---- colour contrast (inline styles only; --render does full check) ---
    if not config.render:
        for f in static_contrast(soup):
            add("Accessibility", "Low colour contrast", WARNING,
                f"Ratio {f['ratio']}:1 (WCAG AA needs {f['required']}:1). Text {f['fg']} on {f['bg']}, "
                f"{f['count']} text block(s), e.g. \"{f['text']}\". (From inline styles.)", f["selector"])

    page.issues.extend(issues)
