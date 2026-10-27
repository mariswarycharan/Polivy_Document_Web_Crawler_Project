"""robots.txt is enforced everywhere; export rules (revisions, duplicates) and the access verdict."""
import pytest
from protego import Protego

import payer_policy_crawler as c
from crawler_io import Fetcher
from crawler_state import PolicyRow


@pytest.fixture
def fetcher():
    f = Fetcher()
    f.robots["https://example.com"] = Protego.parse("User-agent: *\nDisallow: /private/\nDisallow: /search")
    f.robots["https://files.cloudfront.net"] = Protego.parse("User-agent: *\nDisallow: /")
    yield f
    f.close()


def test_robots_rules_apply_to_pages_and_documents_alike(fetcher):
    assert fetcher.allowed("https://example.com/public/page.html")
    assert not fetcher.allowed("https://example.com/private/page.html")
    assert not fetcher.allowed("https://example.com/private/policy.pdf")       # no file-extension exemption
    assert not fetcher.allowed("https://example.com/search?q=policy")
    assert not fetcher.allowed("https://files.cloudfront.net/a.pdf")           # no CDN exemption


def test_a_disallowed_url_is_not_requested(fetcher):
    res = fetcher.fetch("https://example.com/private/policy.pdf")
    assert res.error == "robots_disallowed" and res.status == 0


def _row(url, date, number="MP-1", title="Bariatric Surgery", **kw):
    """Revisions are ordered on the printed last_updated_date (effective_date is not used)."""
    base = dict(payer_name="P", line_of_business="Commercial", state_or_region="National", document_type="medical_policy",
                policy_number=number, document_title=title, http_status=200, effective_date="", last_updated_date=date,
                document_url=url, confidence_score="0.90", notes="", content_hash_sha256="h" + url, source_page_url="s",
                requires_auth="N", file_type="pdf", _upd_src="printed", _owner=0, _fp="", _canon="")
    base.update(kw)
    return base


def test_only_the_latest_dated_revision_is_kept_and_the_rest_are_reported():
    removed = []
    kept = c._revisions([_row("u1", "2023-01-01"), _row("u2", "2024-05-01"), _row("u3", "2022-01-01")], removed)
    assert [r["document_url"] for r in kept] == ["u2"]
    assert {x["removed_document_url"] for x in removed} == {"u1", "u3"}
    assert all(x["rule"] == "superseded_revision" for x in removed)


def test_identical_bytes_at_two_urls_collapse_to_one():
    removed = []
    rows = [_row("u1", "2024-01-01", content_hash_sha256="same"), _row("u2", "2024-01-01", content_hash_sha256="same")]
    for r in rows:
        r.update(_owner=0, _fp="", _canon="", requires_auth="N", confidence_score="0.9", notes="", state_or_region="National",
                 line_of_business="Commercial", last_updated_date="")
    out = c._collapse(rows, lambda r: (r["payer_name"], r["content_hash_sha256"]), "identical_sha256", "same bytes", removed)
    assert len(out) == 1 and len(removed) == 1


@pytest.mark.parametrize("found,pages_ok,blocked,reasons,hosts,robots,expected", [
    (5, 20, 0, {}, [], 0, "FOUND"),
    (5, 20, 2, {"forbidden_403": 2}, [], 0, "FOUND_PARTIAL"),
    (0, 8, 3, {"forbidden_403": 3}, [], 0, "BLOCKED"),
    (0, 8, 0, {}, ["x.com"], 0, "BLOCKED"),
    (0, 8, 0, {}, [], 4, "ROBOTS_RESTRICTED"),
    (0, 0, 0, {}, [], 0, "UNREACHABLE"),
    (0, 30, 0, {}, [], 0, "NO_DOCUMENTS_FOUND"),
])
def test_access_verdict_separates_blocked_from_empty(found, pages_ok, blocked, reasons, hosts, robots, expected):
    assert c.access_verdict(found, pages_ok, blocked, reasons, hosts, robots)[0] == expected


def _valid_row(**kw):
    d = dict(payer_name="P", payer_alias="P Inc", state_or_region="National", line_of_business="Commercial",
             document_title="T", document_type="medical_policy", document_url="https://x.com/a.pdf",
             source_page_url="https://x.com/", discovery_path="seed:x.com > page", file_type="pdf", http_status=200,
             content_hash_sha256="a" * 64, file_size_bytes=10, requires_auth="N", render_mode="static",
             extraction_method="css_selector", confidence_score=0.9, scrape_timestamp_utc="2026-01-01T00:00:00Z")
    d.update(kw)
    return d


def test_schema_rejects_null_words_bad_enums_and_unexplained_low_confidence():
    assert PolicyRow(**_valid_row(policy_number="None")).policy_number == ""        # 'None' becomes an empty string
    with pytest.raises(ValueError):
        PolicyRow(**_valid_row(line_of_business="Dental"))
    with pytest.raises(ValueError):
        PolicyRow(**_valid_row(confidence_score=0.5, notes=""))                      # low confidence must be explained
    with pytest.raises(ValueError):
        PolicyRow(**_valid_row(requires_auth="Y"))                                   # Y rows carry no hash


def test_rows_accepted_before_robots_enforcement_are_taken_out_on_resume(tmp_path):
    from crawler_state import StateDB
    db = StateDB(str(tmp_path / "state.db"))
    for url in ("https://example.com/ok/a.pdf", "https://example.com/private/b.pdf"):
        db.save_doc("P", url, url, True, {"document_url": url}, {}, "crawl")
    crawler = object.__new__(c.PayerCrawler)
    crawler.db, crawler.name = db, "P"
    crawler.fx = type("FX", (), {"allowed": staticmethod(lambda u: "/private/" not in u)})()
    assert crawler.revalidate_robots() == 1
    assert db.accepted_count("P") == 1
    skipped = db.con.execute("SELECT url, reason FROM skipped_links WHERE payer='P'").fetchall()
    assert [r["url"] for r in skipped] == ["https://example.com/private/b.pdf"]
    assert skipped[0]["reason"].startswith("robots.txt disallows")


def _crawler_for_brand(name="Elevance / Anthem"):
    from crawler_config import PAYER_REGISTRY
    p = next(x for x in PAYER_REGISTRY if x["payer_name"] == name)
    cr = object.__new__(c.PayerCrawler)
    own = c._brand_terms([p["payer_name"], p["payer_alias"]] + p["brand_terms"])
    cr.brand_strong_rx = c._brand_rx([t for t in own if c._strong_brand(t)])
    cr.brand_weak_rx = c._brand_rx([t for t in own if not c._strong_brand(t)])
    others = c._brand_terms([t for q in PAYER_REGISTRY if q["payer_name"] != name
                             for t in [q["payer_name"], q.get("payer_alias", "")] + q["brand_terms"]])
    cr.other_brand_rx = c._brand_rx([t for t in others if c._strong_brand(t) and t.lower() not in {x.lower() for x in own}])
    return cr


def test_external_document_must_name_the_payer_itself():
    from crawler_io import Inspection
    cr = _crawler_for_brand()
    ama = Inspection(text="AMA CPT guidance on prior authorization reform. " * 40, heading_title="Prior Authorization Reform")
    assert not cr._brand_named(ama, "Prior authorization reform (AMA)")
    own = Inspection(text="Anthem Blue Cross and Blue Shield Clinical UM Guideline " * 5, heading_title="CG-SURG-01")
    assert cr._brand_named(own, "Clinical guideline")
    passing = Inspection(text=("General policy text. " * 200) + " see Anthem for details")
    assert not cr._brand_named(passing, "Policy")          # one short mention deep in the body is not ownership
    sig = __import__("inspect").signature(c.PayerCrawler._brand_named)
    assert list(sig.parameters) == ["self", "ins", "anchor"]  # the listing page's heading / URL are not evidence


def test_unreachable_robots_is_reported_as_unreachable_not_as_a_rule(fetcher):
    fetcher.robots["https://gone.example.com"] = Protego.parse("User-agent: *\nDisallow: /")
    fetcher.robots_status["https://gone.example.com"] = "robots_error_ConnectError_treated_as_disallow_all"
    res = fetcher.fetch("https://gone.example.com/policies")
    assert res.error.startswith("host_unreachable") and res.status == 0
    assert fetcher.fetch("https://example.com/private/x").error == "robots_disallowed"
