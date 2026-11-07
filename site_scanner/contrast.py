"""WCAG colour-contrast checks.

Two modes:
* static  - only colours set in inline ``style`` attributes (fast, limited).
* render  - loads each page in headless Chromium (Playwright) and checks the computed
            colours of every visible text element (accurate, slower).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "orange": (255, 165, 0), "purple": (128, 0, 128),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "maroon": (128, 0, 0),
    "navy": (0, 0, 128), "teal": (0, 128, 128), "olive": (128, 128, 0), "lime": (0, 255, 0),
    "aqua": (0, 255, 255), "cyan": (0, 255, 255), "fuchsia": (255, 0, 255), "magenta": (255, 0, 255),
    "pink": (255, 192, 203), "brown": (165, 42, 42), "gold": (255, 215, 0), "beige": (245, 245, 220),
    "ivory": (255, 255, 240), "khaki": (240, 230, 140), "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211), "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "gainsboro": (220, 220, 220), "whitesmoke": (245, 245, 245), "lightyellow": (255, 255, 224),
    "lightblue": (173, 216, 230), "lightgreen": (144, 238, 144), "lightpink": (255, 182, 193),
    "darkblue": (0, 0, 139), "darkred": (139, 0, 0), "darkgreen": (0, 100, 0), "dimgray": (105, 105, 105),
    "dimgrey": (105, 105, 105), "slategray": (112, 128, 144), "lavender": (230, 230, 250),
    "coral": (255, 127, 80), "salmon": (250, 128, 114), "tomato": (255, 99, 71), "violet": (238, 130, 238),
    "indigo": (75, 0, 130), "crimson": (220, 20, 60), "tan": (210, 180, 140), "skyblue": (135, 206, 235),
}

_HEX = re.compile(r"#([0-9a-f]{3,8})\b", re.I)
_RGB = re.compile(r"rgba?\(\s*([\d.]+%?)[\s,]+([\d.]+%?)[\s,]+([\d.]+%?)(?:\s*[,/]\s*([\d.]+%?))?\s*\)", re.I)

Color = tuple[float, float, float, float]


def parse_color(value: str) -> Color | None:
    if not value:
        return None
    v = value.strip().lower().replace("!important", "").strip()
    if v == "transparent":
        return (0, 0, 0, 0.0)
    m = _HEX.search(v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return (r, g, b, a)
    m = _RGB.search(v)
    if m:
        def chan(x: str) -> float:
            return float(x[:-1]) * 2.55 if x.endswith("%") else float(x)
        a_raw = m.group(4)
        if a_raw is None:
            a = 1.0
        else:
            a = float(a_raw[:-1]) / 100 if a_raw.endswith("%") else float(a_raw)
        return (chan(m.group(1)), chan(m.group(2)), chan(m.group(3)), a)
    for token in re.split(r"\s+", v):
        if token in NAMED_COLORS:
            return (*NAMED_COLORS[token], 1.0)
    return None


def blend(top: Color, bottom: Color) -> Color:
    a = top[3]
    return (top[0] * a + bottom[0] * (1 - a), top[1] * a + bottom[1] * (1 - a), top[2] * a + bottom[2] * (1 - a), 1.0)


def _lum(c: Color) -> float:
    def f(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast_ratio(fg: Color, bg: Color) -> float:
    l1, l2 = _lum(fg), _lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def to_hex(c: Color) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(x))) for x in c[:3]))


def parse_style(style: str) -> dict[str, str]:
    out = {}
    for decl in (style or "").split(";"):
        if ":" in decl:
            prop, val = decl.split(":", 1)
            out[prop.strip().lower()] = val.strip()
    return out


def _font_px(value: str) -> float | None:
    m = re.match(r"([\d.]+)\s*(px|pt|em|rem)?", value.strip().lower())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "px"
    return {"px": n, "pt": n * 4 / 3, "em": n * 16, "rem": n * 16}[unit]


SKIP = {"script", "style", "noscript", "template", "head", "title", "svg"}


def static_contrast(soup: BeautifulSoup) -> list[dict]:
    """Check text whose foreground AND background colours are both set by inline styles."""
    findings: dict[tuple, dict] = {}
    body = soup.body or soup
    for s in body.find_all(string=True):
        if isinstance(s, Comment):
            continue
        text = " ".join(s.split())
        if not text:
            continue
        fg = bg = None
        size = None
        bold = False
        skip = False
        chain = []
        for node in s.parents:
            if node.name in SKIP:
                skip = True
                break
            if node.name in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6", "th"):
                bold = True
            chain.append(node)
            style = parse_style(node.get("style", "")) if getattr(node, "attrs", None) else {}
            if fg is None and "color" in style:
                fg = parse_color(style["color"])
            if bg is None:
                raw = style.get("background-color") or style.get("background")
                if raw:
                    c = parse_color(raw)
                    if c is not None and c[3] > 0:
                        bg = c if c[3] >= 1 else blend(c, (255, 255, 255, 1.0))
            if size is None and "font-size" in style:
                size = _font_px(style["font-size"])
            if "font-weight" in style and style["font-weight"].strip() in ("bold", "bolder", "700", "800", "900"):
                bold = True
        if skip or fg is None or bg is None:
            continue
        fg_eff = blend(fg, bg) if fg[3] < 1 else fg
        ratio = contrast_ratio(fg_eff, bg)
        px = size or 16.0
        large = px >= 24 or (px >= 18.66 and bold)
        required = 3.0 if large else 4.5
        if ratio + 1e-6 < required:
            key = (to_hex(fg_eff), to_hex(bg), required)
            el = chain[0] if chain else None
            sel = el.name if el is not None else ""
            if el is not None and el.get("id"):
                sel += "#" + el["id"]
            elif el is not None and el.get("class"):
                sel += "." + ".".join(el["class"][:2])
            f = findings.setdefault(key, {
                "fg": key[0], "bg": key[1], "ratio": round(ratio, 2), "required": required,
                "text": text[:80], "selector": sel, "count": 0,
            })
            f["count"] += 1
    return list(findings.values())


RENDER_JS = r"""
() => {
  const parse = (c) => {
    const m = c && c.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,\/]\s*([\d.]+%?))?\s*\)$/);
    if (!m) return null;
    const a = m[4] === undefined ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
    return [+m[1], +m[2], +m[3], a];
  };
  const blend = (t, b) => [t[0]*t[3] + b[0]*(1-t[3]), t[1]*t[3] + b[1]*(1-t[3]), t[2]*t[3] + b[2]*(1-t[3]), 1];
  const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126*lin(c[0]) + 0.7152*lin(c[1]) + 0.0722*lin(c[2]);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const hex = (c) => '#' + c.slice(0, 3).map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
  const sel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
    return cls.length ? s + '.' + cls.join('.') : s;
  };
  const bgOf = (el) => {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const c = parse(cs.backgroundColor);
      if (c === null) return null;
      if (c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let base = [255, 255, 255, 1];
    for (let i = layers.length - 1; i >= 0; i--) base = blend(layers[i], base);
    return base;
  };
  const out = [];
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode;
    const text = t.textContent.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const el = t.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION'].includes(el.tagName)) continue;
    if (el.closest('[disabled],[aria-disabled="true"]')) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    const fg = parse(cs.color);
    if (!fg) continue;
    const bg = bgOf(el);
    if (!bg) continue;
    const fgEff = fg[3] < 1 ? blend(fg, bg) : fg;
    const rat = ratio(fgEff, bg);
    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const required = (size >= 24 || (size >= 18.66 && weight >= 700)) ? 3 : 4.5;
    if (rat + 1e-6 < required) {
      out.push({ text: text.slice(0, 80), fg: hex(fgEff), bg: hex(bg), ratio: Math.round(rat * 100) / 100,
                 required, selector: sel(el) });
    }
  }
  return out;
}
"""


def group_contrast(raw: list[dict]) -> list[dict]:
    """Group per-element findings from RENDER_JS by colour pair."""
    grouped: dict[tuple, dict] = {}
    for f in raw:
        key = (f["fg"], f["bg"], f["required"])
        g = grouped.setdefault(key, {**f, "count": 0})
        g["count"] += 1
    return list(grouped.values())
