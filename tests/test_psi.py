"""PageSpeed Insights response parsing (no network: uses a trimmed sample in the v5 API format)."""

from site_scanner.models import Page
from site_scanner.psi import _apply, is_public, pick_pages

SAMPLE = {
    "loadingExperience": {
        "overall_category": "SLOW",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4300, "category": "SLOW"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 250, "category": "AVERAGE"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"},
        },
    },
    "lighthouseResult": {
        "categories": {"performance": {"score": 0.42, "auditRefs": [
            {"id": "largest-contentful-paint", "weight": 25, "group": "metrics"},
            {"id": "render-blocking-resources", "weight": 0},
            {"id": "uses-text-compression", "weight": 0},
            {"id": "font-display", "weight": 0},
        ]}},
        "audits": {
            "largest-contentful-paint": {"score": 0.1, "scoreDisplayMode": "numeric", "title": "LCP"},
            "render-blocking-resources": {
                "score": 0.3, "scoreDisplayMode": "metricSavings", "title": "Eliminate render-blocking resources",
                "displayValue": "Potential savings of 1,200 ms", "details": {"overallSavingsMs": 1200},
            },
            "uses-text-compression": {
                "score": 0.5, "scoreDisplayMode": "metricSavings", "title": "Enable text compression",
                "details": {"overallSavingsBytes": 204800},
            },
            "font-display": {"score": 1, "scoreDisplayMode": "metricSavings", "title": "Font display"},
        },
    },
}


def test_psi_apply():
    page = Page(url="https://example.com/", final_url="https://example.com/", is_html=True, title="Home")
    _apply(page, SAMPLE, "mobile")
    issues = {i.issue: i for i in page.issues}
    assert "Real-user LCP is poor" in issues and "4.3 s" in issues["Real-user LCP is poor"].details
    assert "Real-user INP needs improvement" in issues
    assert not any("CLS" in name for name in issues)                 # FAST is not reported
    assert issues["Lighthouse performance score"].severity == "Warning"
    assert page.metrics["psi_score"] == 42
    assert "PageSpeed: Eliminate render-blocking resources" in issues
    assert "200 KB" in issues["PageSpeed: Enable text compression"].details
    assert "PageSpeed: LCP" not in issues and "PageSpeed: Font display" not in issues


def test_pick_pages_one_per_section():
    urls = ["https://ex.com/", "https://ex.com/blog/a", "https://ex.com/blog/b", "https://ex.com/shop/x",
            "https://ex.com/about"]
    pages = [Page(url=u, final_url=u, is_html=True) for u in urls]
    depths = {u: i for i, u in enumerate(urls)}
    chosen = [p.final_url for p in pick_pages(pages, depths, 10)]
    assert chosen == ["https://ex.com/", "https://ex.com/blog/a", "https://ex.com/shop/x", "https://ex.com/about"]
    assert not is_public("http://localhost:8000/") and is_public("https://ex.com/")
