"""Phase 3 tests - offline."""
import csv
import sqlite3

from openpyxl import load_workbook

from crawler_io import FetchResult, Inspection
from search_engine.compare import compare, load_crawler_view
from search_engine.gap_export import export
from search_engine.store import url_key
from search_engine.verify import VerifyStore, judge, verify_links

PAYER = "Centene"


def _crawler_out(tmp_path, pending=False):
    out = tmp_path / "crawler_output"
    out.mkdir()
    with (out / "output.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["payer_name", "document_url"])
        w.writeheader()
        w.writerow({"payer_name": PAYER, "document_url": "https://www.centene.com/policies/a.pdf"})
        w.writerow({"payer_name": "Other", "document_url": "https://www.centene.com/policies/z.pdf"})
    con = sqlite3.connect(out / "crawl_state_v2.db")
    con.execute("create table frontier (payer, url, kind, status, reason)")
    con.execute("create table skipped_links (payer, url, reason)")
    con.executemany("insert into frontier values (?,?,?,?,?)", [
        (PAYER, "https://www.centene.com/", "page", "done", ""),
        (PAYER, "https://www.centene.com/policies/b.pdf", "doc", "rejected", "no policy evidence"),
        (PAYER, "https://www.centene.com/later", "page", "pending" if pending else "done", ""),
    ])
    con.execute("insert into skipped_links values (?,?,?)", (PAYER, "https://www.centene.com/low.pdf", "score 1.2"))
    con.commit()
    con.close()
    return out


def _link(url, sources=("commoncrawl",)):
    return {"payer": PAYER, "url": url, "url_key": url_key(url), "sources": list(sources), "kind": "docs", "title": ""}


LINKS = [_link("http://centene.com/policies/a.pdf"), _link("https://www.centene.com/policies/b.pdf"),
         _link("https://www.centene.com/low.pdf"), _link("https://www.centene.com/new/c.pdf"),
         _link("https://www.ambetterhealth.com/d.pdf")]


def test_links_are_split_into_both_seen_and_search_only(tmp_path):
    c = compare(PAYER, LINKS, load_crawler_view(PAYER, _crawler_out(tmp_path)))
    assert [r["url"] for r in c.found_by_both] == ["http://centene.com/policies/a.pdf"]
    assert {r["crawler_status"].split(":")[0] for r in c.seen_by_crawler} == {"rejected", "skipped_low_score"}
    reasons = {r["url"]: r["why_missed"] for r in c.search_only}
    assert reasons == {"https://www.centene.com/new/c.pdf": "not linked from any page the crawler visited",
                       "https://www.ambetterhealth.com/d.pdf": "host never crawled"}
    assert c.overlap_pct == 100.0


def test_unfinished_crawl_is_named_as_the_reason(tmp_path):
    c = compare(PAYER, LINKS, load_crawler_view(PAYER, _crawler_out(tmp_path, pending=True)))
    assert any(r["why_missed"].startswith("not reached - crawl not finished") for r in c.search_only)


def test_payer_without_crawler_output_is_reported_as_such(tmp_path):
    c = compare("Cigna", [_link("https://www.cigna.com/x.pdf")], load_crawler_view("Cigna", _crawler_out(tmp_path)))
    assert c.search_only[0]["why_missed"] == "no crawler output for this payer yet"


def _ins(file_type, text, **kw):
    i = Inspection()
    i.file_type, i.text, i.readable = file_type, text, bool(text)
    for k, v in kw.items():
        setattr(i, k, v)
    return i


POLICY_TEXT = ("Medical Policy. Policy Number: CP.MP.107. Effective Date: 01/01/2026. Coverage criteria: the service is "
               "medically necessary when ... CPT codes 27447 27486 27487 27488 27446 apply. Prior authorization required.")


def test_policy_pdf_is_accepted_and_typed():
    v = judge(_link("https://www.centene.com/new/knee.pdf"), FetchResult(url="u", status=200), _ins("pdf", POLICY_TEXT, heading_title="Knee Arthroplasty"))
    assert v.verdict == "accepted" and v.title == "Knee Arthroplasty" and v.content_score >= 6


def test_unrelated_pdf_is_rejected_and_errors_are_unreachable():
    v = judge(_link("https://www.centene.com/x/newsletter.pdf"), FetchResult(url="u", status=200),
              _ins("pdf", "Community newsletter. Join our picnic and summer events for members."))
    assert v.verdict == "rejected"
    v2 = judge(_link("https://www.centene.com/gone.pdf"), FetchResult(url="u", status=404), None)
    assert v2.verdict == "unreachable" and v2.reason == "HTTP 404"


def test_verification_resumes_and_gap_files_are_written(tmp_path):
    c = compare(PAYER, LINKS, load_crawler_view(PAYER, _crawler_out(tmp_path)))
    class F:
        calls = 0
        def fetch(self, url):
            F.calls += 1
            return FetchResult(url=url, final_url=url, status=200, content=b"x", content_type="application/pdf")
    store = VerifyStore(PAYER, tmp_path)
    inspect = lambda data, ct, url, alias: _ins("pdf", POLICY_TEXT)
    assert verify_links(c.search_only, "Centene", store, max_links=1, fetcher=F(), inspect=inspect, log=lambda *a: None) == 1
    assert verify_links(c.search_only, "Centene", VerifyStore(PAYER, tmp_path), fetcher=F(), inspect=inspect, log=lambda *a: None) == 1
    assert F.calls == 2
    gap, final = export([c], {PAYER: VerifyStore(PAYER, tmp_path).rows}, tmp_path)
    wb = load_workbook(gap)
    assert wb.sheetnames == ["summary", "new_policy_documents", "search_only_other", "seen_by_crawler", "found_by_both"]
    row = [x.value for x in wb["summary"][2]]
    assert row[:8] == [PAYER, 1, 5, 1, 100.0, 2, 2, 2]
    res = load_workbook(final)
    assert res.sheetnames == ["confirmed_by_rules", "accepted_by_local_AI"]
    assert res["confirmed_by_rules"].max_row == 3 and res["accepted_by_local_AI"].max_row == 1


def test_connection_errors_are_not_saved_and_stop_the_run(tmp_path):
    from types import SimpleNamespace
    from search_engine.verify import NetworkDown, VerifyStore, verify_links

    class DeadFetcher:
        def fetch(self, url):
            return SimpleNamespace(error="ConnectError", blocked="", status=0, final_url="", content=b"",
                                   content_type="")
        def close(self):
            pass

    links = [{"url_key": f"x.com/{i}.pdf", "url": f"https://x.com/{i}.pdf"} for i in range(30)]
    store = VerifyStore("Test", out_dir=tmp_path)
    import pytest
    with pytest.raises(NetworkDown):
        verify_links(links, "Test", store, fetcher=DeadFetcher(), log=lambda *_: None)
    assert store.rows == {}


def test_old_connection_failures_are_retried(tmp_path):
    import json
    from search_engine.verify import VerifyStore
    f = tmp_path / "verified_Test.jsonl"
    f.write_text(json.dumps({"url_key": "a", "verdict": "unreachable", "reason": "ConnectError"}) + "\n" +
                 json.dumps({"url_key": "b", "verdict": "unreachable", "reason": "HTTP 404"}) + "\n", encoding="utf-8")
    assert set(VerifyStore("Test", out_dir=tmp_path).rows) == {"b"}


def test_redirect_to_a_dead_host_is_saved_as_a_dead_link(tmp_path):
    from types import SimpleNamespace
    from search_engine.verify import VerifyStore, verify_links

    class Client:
        def get(self, url, follow_redirects=True):
            if "retired" in url:
                raise OSError("getaddrinfo failed")
            return SimpleNamespace(status_code=301, headers={"location": "https://old.retired.example/x.pdf"},
                                   close=lambda: None)

    class RedirectingFetcher:
        client = Client()
        def fetch(self, url):
            return SimpleNamespace(error="ConnectError", blocked="", status=0, final_url="", content=b"",
                                   content_type="")
        def close(self):
            pass

    links = [{"url_key": f"x.com/{i}.pdf", "url": f"https://x.com/{i}.pdf"} for i in range(15)]
    store = VerifyStore("Test", out_dir=tmp_path)
    assert verify_links(links, "Test", store, fetcher=RedirectingFetcher(), log=lambda *_: None) == 15
    row = store.rows["x.com/0.pdf"]
    assert row["verdict"] == "unreachable" and "old.retired.example" in row["reason"]
    assert set(VerifyStore("Test", out_dir=tmp_path).rows) == set(store.rows)   # kept on reload, not retried


def test_verify_order_and_skips():
    from search_engine.verify import newest_first, skip_reason
    links = [{"url": "a", "capture_date": "20060101"}, {"url": "b", "capture_date": "20250101"}, {"url": "c"}]
    assert [l["url"] for l in newest_first(links)] == ["c", "b", "a"]
    assert skip_reason("https://careers.elevancehealth.com/x")
    assert skip_reason("https://www.anthem.com/careers/nurse")
    assert not skip_reason("https://www.anthem.com/provider/policies/clinical.pdf")


def test_redirect_chain_names_the_dead_host():
    from types import SimpleNamespace
    from search_engine.verify import dead_redirect

    class Client:
        def get(self, url, follow_redirects=True):
            if "old.example" in url:
                raise OSError("getaddrinfo failed")
            loc = {"https://x.com/a.pdf": "https://x.com/b.pdf", "https://x.com/b.pdf": "https://old.example/b.pdf"}[url]
            return SimpleNamespace(status_code=301, headers={"location": loc}, close=lambda: None)

    why = dead_redirect(SimpleNamespace(client=Client()), "https://x.com/a.pdf")
    assert "old.example" in why and "retired" in why


def test_strict_review_rejects_and_downgrades():
    from search_engine.verify import strict_review
    acc = dict(url="https://x.com/docs/maryland-member-authorization-form-english.pdf", verdict="accepted",
               reason="ok", title="Instructions for Completing the Member Authorization Form", document_type="pa_form",
               confidence=0.68, link_score=10.7, content_score=2.5, file_type="pdf")
    out = strict_review(acc)
    assert out["verdict"] == "rejected" and out["crawler_verdict"] == "accepted" and "HIPAA" in out["reason"]
    assert strict_review(out)["verdict"] == "rejected"                        # idempotent
    weak = dict(acc, url="https://x.com/a/IA_Thing.pdf", title="", document_type="other", confidence=0.6)
    assert strict_review(weak)["verdict"] == "ambiguous"
    good = dict(acc, url="https://x.com/medpolicies/gl_pw_e001737.html", title="CG-MED-90 Chelation Therapy",
                document_type="coverage_guideline", confidence=0.75)
    assert strict_review(good)["verdict"] == "accepted"
    dental = dict(good, url="https://x.com/forms/MULTI-WP-dental-claims-submission.pdf", title="Dental")
    assert strict_review(dental)["verdict"] == "accepted"
    assert strict_review(dict(acc, verdict="unreachable"))["verdict"] == "unreachable"


def _mixed_fetcher(network_up=True):
    from types import SimpleNamespace

    class Client:
        def get(self, url, follow_redirects=True):
            if "www11." in url or not network_up and "good." in url:
                raise OSError("getaddrinfo failed")
            return SimpleNamespace(status_code=200, headers={}, close=lambda: None)

    class F:
        client = Client()
        calls = []

        def fetch(self, url):
            F.calls.append(url)
            if "www11." in url:
                return SimpleNamespace(error="ConnectError", blocked="", status=0, final_url="", content=b"",
                                       content_type="")
            return SimpleNamespace(error="", blocked="", status=404, final_url=url, content=b"", content_type="")

        def close(self):
            pass
    return F()


def test_retired_host_does_not_stop_the_run(tmp_path):
    from search_engine.verify import VerifyStore, verify_links
    links = [{"url_key": "good.com/a", "url": "https://good.com/a", "capture_date": "20250101"}] + \
            [{"url_key": f"www11.x.com/{i}", "url": f"https://www11.x.com/{i}.pdf", "capture_date": "20240101"}
             for i in range(15)]
    store = VerifyStore("Test", out_dir=tmp_path)
    f = _mixed_fetcher()
    assert verify_links(links, "Test", store, fetcher=f, inspect=lambda *a: None, log=lambda *_: None) == 16
    row = store.rows["www11.x.com/3"]
    assert row["verdict"] == "unreachable" and "retired host" in row["reason"]
    assert sum("www11." in u for u in f.calls) == 1               # later links on that host are not fetched
    assert "www11.x.com/3" in VerifyStore("Test", out_dir=tmp_path).rows   # saved, not retried


def test_real_outage_still_stops(tmp_path):
    import pytest
    from search_engine.verify import NetworkDown, VerifyStore, verify_links
    links = [{"url_key": "good.com/a", "url": "https://good.com/a", "capture_date": "20250101"}] + \
            [{"url_key": f"www11.x.com/{i}", "url": f"https://www11.x.com/{i}.pdf", "capture_date": "20240101"}
             for i in range(15)]
    with pytest.raises(NetworkDown):
        verify_links(links, "Test", VerifyStore("Test", out_dir=tmp_path), fetcher=_mixed_fetcher(network_up=False),
                     inspect=lambda *a: None, log=lambda *_: None)


def test_trailing_dot_host_is_cleaned():
    from search_engine.verify import clean_url
    assert clean_url("https://www.anthem.com./a.pdf") == "https://www.anthem.com/a.pdf"
    assert clean_url("https://www.anthem.com/a.pdf") == "https://www.anthem.com/a.pdf"
