"""Interrupt a scan part-way through, resume it, and check the result matches an uninterrupted scan."""

from __future__ import annotations

import functools
import threading
from http.server import ThreadingHTTPServer

import pytest

from site_scanner.crawler import Scanner
from site_scanner.models import Config
from site_scanner.state import StateStore
from test_scan import SITE_DIR, Handler

HITS: list[str] = []


class CountingHandler(Handler):
    def do_GET(self):
        HITS.append(self.path)
        super().do_GET()

    def do_HEAD(self):
        HITS.append(self.path)
        super().do_HEAD()


@pytest.fixture(scope="module")
def site_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(CountingHandler, directory=str(SITE_DIR)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def make_config(url):
    return Config(start_url=url, workers=2, timeout=5, check_external=False, spelling=True)


def key(result):
    return sorted((i.page_url, i.issue, i.target, i.severity) for i in result.issues)


def test_interrupt_and_resume(site_url, tmp_path):
    reference = Scanner(make_config(site_url), log=lambda m: None).run()

    state_dir = tmp_path / "state"
    crawled = []

    def interrupting_log(msg):
        if msg.startswith("[crawl"):
            crawled.append(msg)
            if len(crawled) == 5:
                raise KeyboardInterrupt

    store = StateStore(state_dir, {"v": 1}, resume=False, log=lambda m: None)
    scanner = Scanner(make_config(site_url), log=interrupting_log, state=store)
    with pytest.raises(KeyboardInterrupt):
        scanner.run()
    store.finish(complete=False)
    saved_pages = len(StateStore._load(state_dir / "pages.jsonl"))
    assert saved_pages >= 5

    HITS.clear()
    messages = []
    store = StateStore(state_dir, {"v": 1}, resume=True, log=messages.append)
    resumed = Scanner(make_config(site_url), log=messages.append, state=store).run()
    store.finish(complete=True)

    assert any(m.startswith("Resuming saved scan") for m in messages)
    assert sum("(saved)" in m for m in messages) >= 5
    assert key(resumed) == key(reference)
    # pages saved before the interruption were not fetched again
    refetched = [h for h in HITS if h.endswith(".html") and h != "/slow.html"]
    assert len(refetched) <= len(reference.pages) - saved_pages + 3


def test_checkpoint_reports_partial_results(site_url):
    snapshots = []
    Scanner(make_config(site_url), log=lambda m: None, on_checkpoint=snapshots.append,
            checkpoint_every=0).run()
    assert len(snapshots) > 5                                   # written repeatedly during the scan
    assert len(snapshots[0].issues) <= len(snapshots[-1].issues)
    assert any("Broken link" == i.issue for i in snapshots[-1].issues)
