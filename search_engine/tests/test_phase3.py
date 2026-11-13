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
    assert load_workbook(final)["new_policy_documents"].max_row == 3
