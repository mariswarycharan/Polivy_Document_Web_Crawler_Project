"""Phase 1 tests - run offline: python -m pytest search_engine/tests"""
import httpx
import pytest

from search_engine import archive_client
from search_engine.payers import find_payer, from_registry
from search_engine.query_builder import build_queries
from search_engine.searxng_client import SearxClient

REGISTRY = [
    {"payer_name": "Centene", "hint_host": "centene.com",
     "affiliated_domains": ["centene.com", "https://www.Ambetterhealth.com/", "wellcare.com"], "brand_terms": ["Centene"]},
    {"payer_name": "UHC", "hint_host": "uhcprovider.com", "affiliated_domains": ["uhc.com"]},
]


def test_registry_domains_are_cleaned_and_deduplicated():
    p = from_registry(REGISTRY)[0]
    assert p.domains == ("centene.com", "ambetterhealth.com", "wellcare.com")


def test_payer_found_by_name_or_domain():
    payers = from_registry(REGISTRY)
    assert find_payer(payers, "centene").name == "Centene"
    assert find_payer(payers, "www.uhc.com").name == "UHC"
    with pytest.raises(SystemExit):
        find_payer(payers, "nobody")


def test_queries_cover_every_domain_term_and_filetype():
    p = from_registry(REGISTRY)[0]
    qs = build_queries(p, terms=["medical policy", "formulary"], file_types=["pdf"])
    assert len(qs) == 3 * 2 * 2
    assert qs[0].text == 'site:centene.com "medical policy"'
    assert qs[1].text == 'site:centene.com "medical policy" filetype:pdf'


def _client(handler):
    return SearxClient(base_url="http://sx.test", delay=0, transport=httpx.MockTransport(handler))


def test_results_and_unresponsive_engines_are_read():
    def handler(req):
        assert req.url.params["format"] == "json" and req.url.params["engines"] == "duckduckgo"
        return httpx.Response(200, json={"results": [{"url": "https://a.com/p.pdf", "title": "P", "engine": "duckduckgo"}],
                                         "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]})
    resp = _client(handler).search_page("q")
    assert [r.url for r in resp.results] == ["https://a.com/p.pdf"]
    assert resp.unresponsive == ["duckduckgo: CAPTCHA"]


def test_json_disabled_gives_clear_error():
    resp = _client(lambda req: httpx.Response(403)).search_page("q")
    assert "search.formats" in resp.error


def test_paging_stops_when_a_page_repeats():
    calls = []
    def handler(req):
        calls.append(req.url.params["pageno"])
        return httpx.Response(200, json={"results": [{"url": "https://a.com/1"}]})
    resp = _client(handler).search("q", max_pages=3)
    assert len(resp.results) == 1 and calls == ["1", "2"]


def test_unreachable_searxng_is_reported():
    def handler(req):
        raise httpx.ConnectError("refused")
    assert "cannot reach SearXNG" in _client(handler).search_page("q").error


@pytest.mark.parametrize("url,mime,keep", [
    ("https://x.com/docs/a.pdf", "application/pdf", True),
    ("https://x.com/files/B.PDF", "", True),
    ("https://x.com/medical-policies/knee.html", "text/html", True),
    ("https://x.com/about-us", "text/html", False),
    ("https://x.com/logo.png", "image/png", False),
])
def test_archive_candidate_filter(url, mime, keep):
    assert archive_client.is_candidate(url, mime) is keep


def test_archive_listing_uses_subdomain_pattern_and_status_filter():
    seen = {}
    class FakeFetcher:
        def iter(self, pattern, limit, filter):
            seen.update(pattern=pattern, filter=filter)
            yield {"url": "https://www.x.com/p/a.pdf", "mime": "application/pdf", "status": "200", "timestamp": "20260101000000"}
            yield {"url": "https://www.x.com/contact", "mime": "text/html", "status": "200", "timestamp": "20260101000000"}
    hits = list(archive_client.list_domain("x.com", "cc", limit=10, fetcher=FakeFetcher()))
    assert seen == {"pattern": "*.x.com", "filter": ["=status:200"]}
    assert [h.url for h in hits] == ["https://www.x.com/p/a.pdf"] and hits[0].source == "commoncrawl"
