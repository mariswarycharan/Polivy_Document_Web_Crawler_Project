"""Phase 2 tests - offline."""
import httpx
from openpyxl import load_workbook

from search_engine.collector import CollectReport, collect_archive, collect_searxng
from search_engine.excel_export import export
from search_engine.payers import from_registry
from search_engine.searxng_client import SearxClient
from search_engine.store import LinkStore

PAYER = from_registry([{"payer_name": "Centene", "hint_host": "centene.com", "affiliated_domains": ["ambetterhealth.com"]}])[0]


def test_same_link_from_two_sources_is_stored_once(tmp_path):
    st = LinkStore("Centene", tmp_path)
    assert st.add("https://www.centene.com/a.pdf", "commoncrawl", kind="docs")
    assert not st.add("http://centene.com/a.pdf/", "wayback", capture_date="20250101")
    st.save()
    again = LinkStore("Centene", tmp_path)
    (row,) = again.rows.values()
    assert row["sources"] == ["commoncrawl", "wayback"] and row["capture_date"] == "20250101"


def test_archive_collection_covers_every_domain_and_survives_a_failing_one(tmp_path):
    class Fetcher:
        def iter(self, pattern, limit, filter):
            if "ambetter" in pattern:
                raise ConnectionError("index down")
            yield {"url": "https://www.centene.com/policies/x.pdf", "mime": "application/pdf", "status": "200", "timestamp": "20260102030405"}
    st, rep = LinkStore("Centene", tmp_path), CollectReport("Centene")
    collect_archive(PAYER, st, rep, sources=["cc"], fetcher_factory=lambda s: Fetcher(), log=lambda *a: None)
    assert len(st.rows) == 1 and rep.archive_errors and "ambetterhealth.com" in rep.archive_errors[0]


def test_searxng_batch_stops_on_captcha_and_resumes_later(tmp_path):
    state = {"captcha": False}
    def handler(req):
        if state["captcha"]:
            return httpx.Response(200, json={"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]})
        q = req.url.params["q"]
        return httpx.Response(200, json={"results": [{"url": f"https://www.centene.com/{abs(hash(q))}.pdf", "engine": "duckduckgo"}]})
    client = SearxClient(base_url="http://sx.test", delay=0, transport=httpx.MockTransport(handler))
    st, rep = LinkStore("Centene", tmp_path), CollectReport("Centene")
    collect_searxng(PAYER, st, rep, client=client, max_queries=3, max_pages=1, out_dir=tmp_path, log=lambda *a: None)
    assert rep.searxng_queries_run == 3 and len(st.rows) == 3
    state["captcha"] = True
    rep2 = CollectReport("Centene")
    collect_searxng(PAYER, st, rep2, client=client, max_queries=3, max_pages=1, out_dir=tmp_path, log=lambda *a: None)
    assert rep2.searxng_queries_run == 0 and "CAPTCHA" in rep2.searxng_stopped
    assert rep2.searxng_queries_left == rep.searxng_queries_left   # nothing was marked done


def test_excel_has_links_and_summary(tmp_path):
    st = LinkStore("Centene", tmp_path)
    st.add("https://www.centene.com/a.pdf", "commoncrawl", kind="docs", title="Bad\x0bchar")
    st.add("https://www.centene.com/policies/b.html", "searxng", kind="pages")
    st.save()
    wb = load_workbook(export(tmp_path))
    assert wb.sheetnames == ["links", "summary"]
    assert wb["links"].max_row == 3
    assert [c.value for c in wb["summary"][2]] == ["Centene", 2, 1, 0, 1, 0, 1, 1]


def test_index_server_errors_give_up_after_three_tries():
    import pytest
    from search_engine import archive_client as ac
    class Resp:
        def __init__(self, code): self.status_code = code
    class Real:
        calls = 0
        def get(self, url, params=None, **kw):
            Real.calls += 1
            return Resp(504)
    shim = ac._GiveUpRequests(Real())
    for _ in range(ac.MAX_SERVER_ERRORS - 1):
        assert shim.get("https://index.test", params={"url": "*.x.com"}).status_code == 504
    with pytest.raises(ac.ArchiveUnavailable):
        shim.get("https://index.test", params={"url": "*.x.com"})


def test_a_skipped_pass_is_logged_and_the_next_domain_still_runs(tmp_path):
    from search_engine.archive_client import ArchiveUnavailable
    class Fetcher:
        def iter(self, pattern, limit, filter):
            if "centene" in pattern:
                raise ArchiveUnavailable("index server answered HTTP 504 3 times in a row")
            yield {"url": "https://www.ambetterhealth.com/x.pdf", "mime": "application/pdf", "status": "200", "timestamp": "20260101000000"}
    lines = []
    st, rep = LinkStore("Centene", tmp_path), CollectReport("Centene")
    collect_archive(PAYER, st, rep, sources=["cc"], fetcher_factory=lambda s: Fetcher(), log=lines.append)
    assert len(st.rows) == 1 and len(rep.archive_errors) == 2
    assert any("SKIPPED" in l and "centene.com" in l for l in lines)


def test_index_server_timeouts_also_give_up_after_three_tries():
    import pytest
    import requests
    from search_engine import archive_client as ac
    class Real:
        exceptions = requests.exceptions
        def get(self, url, params=None, **kw):
            raise requests.exceptions.ReadTimeout("read timed out")
    shim = ac._GiveUpRequests(Real())
    for _ in range(ac.MAX_SERVER_ERRORS - 1):
        with pytest.raises(requests.exceptions.ReadTimeout):
            shim.get("https://index.test", params={"url": "*.uhc.com"})
    with pytest.raises(ac.ArchiveUnavailable):
        shim.get("https://index.test", params={"url": "*.uhc.com"})
