"""Spell checking of visible page text (dictionary based, via pyspellchecker)."""

from __future__ import annotations

import re
import threading
from typing import Iterable

WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
# Strip things that are not prose before tokenising.
NOISE_RE = re.compile(
    r"(https?://\S+|www\.\S+|\S+@\S+\.\S+|\b[\w-]+\.(?:com|org|net|io|co|uk|gov|edu|html?|php|aspx?)\b\S*)",
    re.I,
)
CONTRACTION_SUFFIXES = ("n't", "'re", "'ve", "'ll", "'d", "'s", "'m")
KNOWN_CONTRACTIONS = {"can't", "won't", "shan't", "ain't", "y'all", "o'clock", "ma'am", "n't"}
# Common web vocabulary that general-purpose dictionaries often lack.
WEB_WORDS = {
    "website", "websites", "webpage", "webpages", "online", "offline", "email", "emails", "emailed",
    "login", "logout", "signup", "username", "usernames", "homepage", "blog", "blogs", "blogging",
    "app", "apps", "ebook", "ebooks", "podcast", "podcasts", "newsletter", "newsletters", "faq", "faqs",
    "checkout", "dropdown", "popup", "smartphone", "smartphones", "wifi", "url", "urls", "html", "css",
    "javascript", "ecommerce", "analytics", "subscribe", "unsubscribe", "cookie", "cookies", "toolbar",
    "sitemap", "hashtag", "hashtags", "livestream", "webinar", "webinars", "startup", "startups",
    "onboarding", "workflow", "workflows", "cybersecurity", "dataset", "datasets", "tooltip", "chatbot",
}


class SpellCheck:
    def __init__(self, language: str = "en", custom_words: Iterable[str] = (), check_capitalized: bool = False):
        try:
            from spellchecker import SpellChecker
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyspellchecker is not installed: pip install pyspellchecker") from exc
        self._sp = SpellChecker(language=language, distance=1)
        extra = {w.strip().lower() for w in custom_words if w.strip()} | WEB_WORDS
        self._sp.word_frequency.load_words(extra)
        self._custom = extra
        self.check_capitalized = check_capitalized
        self._known: dict[str, bool] = {}
        self._suggestions: dict[str, str] = {}
        self._lock = threading.Lock()

    def _in_dict(self, word: str) -> bool:
        return word in self._custom or word in self._sp

    def is_known(self, word: str) -> bool:
        cached = self._known.get(word)
        if cached is not None:
            return cached
        known = self._in_dict(word)
        if not known and "'" in word:
            if word in KNOWN_CONTRACTIONS:
                known = True
            else:
                for suffix in CONTRACTION_SUFFIXES:
                    if word.endswith(suffix):
                        base = word[: -len(suffix)]
                        known = len(base) < 3 or self._in_dict(base)
                        break
        if not known and "'" not in word and word.endswith("s") and len(word) > 4:
            # plurals/possessives written without apostrophe of known custom words
            known = word[:-1] in self._custom
        with self._lock:
            self._known[word] = known
        return known

    def suggest(self, word: str) -> str:
        if word in self._suggestions:
            return self._suggestions[word]
        try:
            suggestion = self._sp.correction(word) or ""
        except Exception:
            suggestion = ""
        if suggestion == word:
            suggestion = ""
        with self._lock:
            self._suggestions[word] = suggestion
        return suggestion

    def find_typos(self, texts: Iterable[str]) -> dict[str, tuple[str, str]]:
        """Return {misspelled word: (suggestion, context snippet)} for the first occurrence of each."""
        found: dict[str, tuple[str, str]] = {}
        for text in texts:
            if "lorem ipsum" in text.lower():
                continue  # reported separately as placeholder text
            cleaned = NOISE_RE.sub(" ", text)
            for m in WORD_RE.finditer(cleaned):
                start, end = m.span()
                if start > 0 and (cleaned[start - 1].isdigit() or cleaned[start - 1] in "_/\\@#$%&.-"):
                    continue
                if end < len(cleaned) and (cleaned[end].isdigit() or cleaned[end] in "_/\\@"):
                    continue
                if end + 1 < len(cleaned) and cleaned[end] in ".-" and cleaned[end + 1].isalnum():
                    continue  # part of a file name, domain or hyphenated code
                word = m.group(0).replace("’", "'").strip("'")
                if len(word) < 3:
                    continue
                sentence_start = False
                if any(c.isupper() for c in word):
                    if word.isupper() or any(c.isupper() for c in word[1:]):
                        continue  # acronym or camelCase
                    before = cleaned[:start].rstrip()
                    sentence_start = not before or before[-1] in ".!?:\"'"
                    if not self.check_capitalized and not sentence_start:
                        continue  # likely a proper noun or brand name
                lw = word.lower()
                if lw in found or self.is_known(lw):
                    continue
                suggestion = self.suggest(lw)
                if sentence_start and not self.check_capitalized and not suggestion:
                    continue  # capitalised with no close match: probably a name
                ctx_start = max(0, start - 35)
                ctx_end = min(len(cleaned), end + 35)
                context = cleaned[ctx_start:ctx_end].strip()
                if ctx_start > 0:
                    context = "..." + context
                if ctx_end < len(cleaned):
                    context += "..."
                found[lw] = (suggestion, context)
        return found
