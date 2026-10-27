"""Pure text rules: URL keys, link scoring, document typing, dates, policy numbers, CDN tenants."""
from crawler_text import (cdn_tenant, classify_doc_type, declared_count, detect_lob, detect_state, excluded_doc_type,
                          extract_dates, extract_policy_number, is_cdn_host, score_link, url_key, url_words)


def test_url_key_ignores_tracking_scheme_and_www():
    a = url_key("https://www.example.com/policies/a.pdf?utm_source=x")
    b = url_key("http://example.com/policies/a.pdf")
    assert a == b


def test_policy_anchor_scores_high_and_privacy_scores_low():
    good, _ = score_link("https://x.com/medical-policies", "Medical Policies", is_page=True)
    bad, _ = score_link("https://x.com/privacy", "Privacy Policy", is_page=True)
    assert good >= 5 > 0 > bad


def test_manual_and_bulletin_penalty_applies_to_documents_not_hub_pages():
    page, _ = score_link("https://x.com/policy-updates", "Policy Updates", is_page=True)
    doc, _ = score_link("https://x.com/policy-updates.pdf", "Policy Updates")
    assert page >= 2          # a "Policy Updates" page can be a hub of real policies: still followed
    assert doc < 0            # the same words on a document link mark it as a bulletin


def test_excluded_types_but_not_clinical_policy_bulletins():
    assert excluded_doc_type("Provider Manual 2025") == "provider_manual"
    assert excluded_doc_type("Network News March 2024 Bulletin") == "bulletin"
    assert excluded_doc_type("Clinical Policy Bulletins 0264") == ""


def test_document_types():
    assert classify_doc_type("Preferred Drug List 2025")[0] in ("drug_list", "formulary")
    assert classify_doc_type("Prior authorization request form")[0] == "pa_form"
    assert classify_doc_type("Medical Policy: Bariatric Surgery")[0] == "medical_policy"


def test_dates_are_labelled_only_and_partial_dates_stay_empty():
    d = extract_dates("Policy Number: MP-1\nEffective Date: 03/01/2024\nLast Reviewed: 02/15/2024")
    assert (d["effective_date"], d["last_updated_date"]) == ("2024-03-01", "2024-02-15")
    d = extract_dates("Effective Date: March 2024")
    assert d["effective_date"] == "" and d["notes"]          # month/year without a day is not turned into a date
    assert extract_dates("A plan year 2026 guide")["effective_date"] == ""     # a bare year is never a date


def test_dates_found_in_the_footer_are_noted():
    d = extract_dates("Medical Policy text with no date", tail="Revision history\nEffective Date: 03/01/2024")
    assert d["effective_date"] == "2024-03-01"
    assert any("end of the document" in n for n in d["notes"])


def test_policy_number_is_never_derived_from_the_file_name():
    assert extract_policy_number("Policy Number: 0264\nMultiple Sclerosis", "Multiple Sclerosis") == ("0264", "labeled")
    assert extract_policy_number("no identifier in the text at all", "Bariatric Surgery") == ("", "")


def test_line_of_business_and_state_are_not_guessed():
    assert detect_lob("Medicare Advantage Policy", "", "")[0] == "Medicare"
    assert detect_lob("Some Policy", "", "")[0] == "Unknown"
    assert detect_state("https://x.com/a.pdf", "Some Policy", "", "", "")[0] == "National"
    assert detect_state("https://x.com/a.pdf", "New Jersey Medical Policy", "", "", "")[0] == "NJ"


def test_declared_count_reads_listing_totals_and_ignores_years():
    assert declared_count("Showing 1-20 of 523 results")[0] == 523
    assert declared_count("523 policies found")[0] == 523
    assert declared_count("Updated 2024 policies")[0] == 0
    assert declared_count("Page 1 of 25")[0] == 0


def test_cdn_tenants():
    assert is_cdn_host("acme.blob.core.windows.net")
    assert is_cdn_host("storage.googleapis.com")
    assert cdn_tenant("https://storage.googleapis.com/acme-docs/a.pdf") == "storage.googleapis.com/acme-docs"
    assert cdn_tenant("https://bucket.s3.amazonaws.com/a.pdf") == "bucket.s3.amazonaws.com"
    assert cdn_tenant("https://www.anthem.com/a.pdf") == ""


def test_url_words_split_camel_case():
    assert "prior authorization" in url_words("https://x.com/PriorAuthList.pdf")


def test_escaped_ampersands_do_not_create_url_variants():
    from crawler_text import normalize_url
    clean = normalize_url("https://x.com/view/a.pdf?t.download=true&u=9k")
    assert normalize_url("https://x.com/view/a.pdf?t.download=true&amp;u=9k") == clean
    assert normalize_url("https://x.com/view/a.pdf?t.download=true&amp%3Bu=9k") == clean
    assert normalize_url(r"https://x.com/view/a.pdf?t.download=true\u0026u=9k\\") == clean


def test_pagination_and_a_to_z_links_of_one_listing():
    from crawler_text import is_pagination_link
    hub = "https://x.com/providers/medical-policies?type=commercial"
    assert is_pagination_link("https://x.com/providers/medical-policies?type=commercial&page=2", hub)
    assert is_pagination_link("https://x.com/providers/medical-policies?type=commercial&letter=B", hub)
    assert is_pagination_link("https://x.com/blog/page/3", "https://x.com/blog")
    assert not is_pagination_link("https://x.com/providers/medical-policies?type=medicare", hub)   # another filter
    assert not is_pagination_link("https://x.com/providers/other-page", hub)
    assert not is_pagination_link("https://y.com/providers/medical-policies?page=2", hub)
