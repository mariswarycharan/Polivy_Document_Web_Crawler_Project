"""Phase 3c: the local AI settles ambiguous links (no Ollama needed - the classifier is faked)."""
from types import SimpleNamespace

from search_engine.ai_review import JsonCache, ai_decide, review_ambiguous, settle
from search_engine.verify import VerifyStore, strict_review


def test_ai_decide_follows_the_crawler_gate3():
    yes = {"is_policy_document": True, "confidence": 0.9, "reason": "clinical criteria"}
    no = {"is_policy_document": False, "confidence": 0.8, "reason": "marketing"}
    assert ai_decide(yes, 1, 0, [])[0] == "accepted"
    assert ai_decide(dict(yes, confidence=0.4), 1, 0, [])[0] == "rejected"          # below 0.5
    assert ai_decide(no, 6, 1, [])[0] == "accepted"                                  # rule override 1
    assert ai_decide(no, 2, 4, [])[0] == "accepted"                                  # rule override 2
    assert ai_decide(no, 5, 2, ["repo_path"])[0] == "accepted"                       # rule override 3
    assert ai_decide(no, 5, 2, [])[0] == "rejected"
    assert ai_decide(None, 5, 0, [])[0] == "accepted" and ai_decide(None, 1, 1, [])[0] == "rejected"


class FakeAI:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def classify(self, payer, url, anchor, context, title, text, is_disambiguation=True):
        self.calls += 1
        return self.answer


def _ins(text="Some text"):
    return SimpleNamespace(file_type="pdf", text=text, heading_title="Clinical Criteria", titles=[])


def test_settled_row_keeps_history_and_is_not_downgraded_again(tmp_path):
    row = dict(url_key="x.com/a.pdf", url="https://x.com/a.pdf", verdict="ambiguous", reason="mixed evidence",
               title="", document_type="other", confidence=0.4, link_score=1.0, content_score=1.0, file_type="pdf")
    ai = FakeAI({"is_policy_document": True, "confidence": 0.9, "reason": "criteria", "document_type": "medical_policy",
                 "line_of_business": "Medicaid"})
    out = settle(strict_review(row), _ins(), "Test", ai)
    assert out["verdict"] == "accepted" and out["pre_ai_verdict"] == "ambiguous"
    assert out["ai_verdict"] == "policy" and out["document_type"] == "medical_policy"
    assert strict_review(out)["verdict"] == "accepted"            # thin-evidence rule does not undo the AI
    store = VerifyStore("Test", out_dir=tmp_path)
    store.put_row(out)
    assert VerifyStore("Test", out_dir=tmp_path).rows["x.com/a.pdf"]["verdict"] == "accepted"


def test_ai_accept_still_hits_the_reject_patterns():
    row = dict(url_key="k", url="https://x.com/ELV_TCFD_Disclosure_2025.pdf", verdict="ambiguous", reason="mixed",
               title="", document_type="other", file_type="pdf", link_score=0, content_score=0)
    out = settle(row, _ins(), "Test", FakeAI({"is_policy_document": True, "confidence": 0.9, "reason": "?"}))
    assert strict_review(out)["verdict"] == "rejected"


def test_review_ambiguous_refetches_and_skips_failed_fetches(tmp_path):
    store = VerifyStore("Test", out_dir=tmp_path)
    rows = [dict(url_key=f"k{i}", url=f"https://x.com/{i}.pdf", verdict="ambiguous", reason="mixed", title="",
                 document_type="other", file_type="pdf", link_score=0, content_score=0) for i in range(3)]

    class F:
        def fetch(self, url):
            bad = url.endswith("2.pdf")
            return SimpleNamespace(error="ConnectError" if bad else "", blocked="", status=0 if bad else 200,
                                   content=b"x", content_type="application/pdf", final_url=url)
        def close(self):
            pass

    ai = FakeAI({"is_policy_document": False, "confidence": 0.9, "reason": "brochure"})
    n = review_ambiguous(rows, "Test", store, ai, fetcher=F(), inspect=lambda *a: _ins(), log=lambda *_: None)
    assert n == 2 and ai.calls == 2
    assert store.rows["k0"]["verdict"] == "rejected" and "k2" not in store.rows


def test_json_cache_round_trip(tmp_path):
    c = JsonCache(tmp_path / "c.jsonl")
    c.llm_put("a", {"is_policy_document": True})
    assert JsonCache(tmp_path / "c.jsonl").llm_get("a") == {"is_policy_document": True}


def test_file_name_is_spelled_out_for_the_ai():
    from search_engine.ai_review import describe_file_name
    d = describe_file_name("https://x.com/a/MDWP_CAID_AUG24CUMG.pdf")
    assert "MDWP_CAID_AUG24CUMG.pdf" in d and "clinical utilization management guidelines" in d and "medicaid" in d
    assert "medicare" not in describe_file_name("https://x.com/a/healthcare-guide.pdf")


def test_ai_gets_the_file_name_when_the_title_is_empty():
    seen = {}

    class Spy(FakeAI):
        def classify(self, payer, url, anchor, context, title, text, is_disambiguation=True):
            seen.update(anchor=anchor, context=context)
            return self.answer

    row = dict(url_key="k", url="https://x.com/MDWP_CAID_AUG24CUMG.pdf", verdict="ambiguous", reason="mixed",
               title="", document_type="other", file_type="pdf", link_score=0, content_score=0)
    settle(row, _ins(), "Test", Spy({"is_policy_document": True, "confidence": 0.9, "reason": "ok"}))
    assert "CUMG" in seen["anchor"] and "clinical utilization management" in seen["context"]


def test_ai_reason_is_checked_by_the_strict_filter():
    row = dict(url_key="k", url="https://x.com/DC_WP_AM_HHSNotice.pdf", verdict="ambiguous", reason="mixed",
               title="", document_type="other", file_type="pdf", link_score=0, content_score=0)
    out = settle(row, _ins(), "Test", FakeAI({"is_policy_document": True, "confidence": 0.95,
                                              "reason": "Notice of Privacy Practices with prior authorization info"}))
    assert out["verdict"] == "accepted" and strict_review(out)["verdict"] == "rejected"


def test_older_ai_answers_are_asked_again_and_keep_the_first_rule_verdict():
    from search_engine.ai_review import needs_ai
    from search_engine.config import AI_PROMPT_VERSION
    old = dict(url_key="k", url="https://x.com/a.pdf", verdict="rejected", ai_verdict="not policy", ai_version=1,
               pre_ai_verdict="ambiguous", pre_ai_reason="mixed evidence", title="", document_type="other",
               file_type="pdf", link_score=0, content_score=0)
    assert needs_ai(old) and not needs_ai(dict(old, ai_version=AI_PROMPT_VERSION))
    out = settle(old, _ins(), "Test", FakeAI({"is_policy_document": True, "confidence": 0.9, "reason": "ok"}))
    assert out["pre_ai_verdict"] == "ambiguous" and out["ai_version"] == AI_PROMPT_VERSION
