"""Phase 3b: checks each search-only link with the crawler's own fetcher and decision rules.

Verdicts: accepted (a policy document), rejected (not a policy document), ambiguous (mixed evidence -
needs a human look; the crawler would ask its local LLM), unreachable (error, block, HTTP >= 400).
Results are kept per payer in output/verified_<payer>.jsonl, so a later run continues where it stopped.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import OUTPUT_DIR
from .store import now_utc, slug

# Errors that mean "this machine could not reach the site", not "the link is bad". They are never saved as
# a final verdict, so a later run retries them; a run stops early when they repeat back to back.
NETWORK_ERRORS = ("ConnectError", "ConnectTimeout", "NetworkError", "host_unreachable")
STOP_AFTER_NETWORK_ERRORS = 10


class NetworkDown(RuntimeError):
    """Raised when many links in a row fail to connect - the network or DNS is down, not the links."""


def is_network_error(reason: str) -> bool:
    return any(reason.startswith(e) for e in NETWORK_ERRORS)


@dataclass
class Verdict:
    url_key: str
    url: str
    verdict: str            # accepted / rejected / ambiguous / unreachable
    reason: str
    http_status: int = 0
    file_type: str = ""
    title: str = ""
    document_type: str = ""
    content_score: float = 0.0
    link_score: float = 0.0
    confidence: float = 0.0
    final_url: str = ""
    checked_at: str = ""


def _title(ins, fallback: str) -> str:
    for t in list(getattr(ins, "titles", []) or []) + [getattr(ins, "heading_title", ""), getattr(ins, "meta_title", "")]:
        if t and t.strip():
            return t.strip()[:300]
    return fallback


def judge(link: dict, fetch_result, ins) -> Verdict:
    """Pure decision step (no network) - the same gates the crawler applies to a fetched document."""
    from crawler_text import classify_doc_type, content_fingerprint, html_policy_evidence, score_link, url_words
    from payer_policy_crawler import PayerCrawler

    r = fetch_result
    base = dict(url_key=link["url_key"], url=link["url"], http_status=r.status, final_url=r.final_url or "",
                checked_at=now_utc())
    if r.error or r.blocked or not r.status or r.status >= 400:
        why = r.blocked or r.error or f"HTTP {r.status}"
        return Verdict(verdict="unreachable", reason=why, **base)
    title = _title(ins, link.get("title", ""))
    ls, _ = score_link(link["url"], anchor=link.get("title", "") or title, is_page=(ins.file_type == "html"))
    if ins.file_type == "html":
        ok, cs, _ = html_policy_evidence(ins)
        verdict, conf, why = (("accepted", 0.75, "web page with policy evidence") if ok else
                              ("rejected", 0.0, "web page without policy evidence (index, hub or general page)"))
    else:
        cs, _ = content_fingerprint(ins.text)
        decision, conf, why = PayerCrawler._decide(ls, cs, ins.readable, ins.file_type)
        verdict = {"accept": "accepted", "reject": "rejected"}.get(decision, "ambiguous")
    dtype = classify_doc_type(f"{title} {url_words(link['url'])}", ins.text[:400], ins.text[400:3000])[0]
    return Verdict(verdict=verdict, reason=why, file_type=ins.file_type, title=title, document_type=dtype,
                   content_score=round(cs, 2), link_score=round(ls, 2), confidence=round(conf, 2), **base)


class VerifyStore:
    def __init__(self, payer: str, out_dir: Path = OUTPUT_DIR) -> None:
        self.path = Path(out_dir) / f"verified_{slug(payer)}.jsonl"
        self.rows: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    v = json.loads(line)
                    if v.get("verdict") == "unreachable" and is_network_error(v.get("reason", "")):
                        self.rows.pop(v["url_key"], None)       # connection failure: retry it next run
                        continue
                    self.rows[v["url_key"]] = v

    def put(self, v: Verdict) -> None:
        self.rows[v.url_key] = asdict(v)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(v), ensure_ascii=False) + "\n")


def verify_links(links: list[dict], payer_alias: str, store: VerifyStore, max_links: int | None = None,
                 fetcher=None, inspect=None, log=print) -> int:
    """Verify links not yet in the store. Returns how many were checked in this run."""
    todo = [l for l in links if l["url_key"] not in store.rows]
    if max_links is not None:
        todo = todo[:max_links]
    if not todo:
        return 0
    own = fetcher is None
    if own:
        from crawler_io import Fetcher
        fetcher = Fetcher()
    if inspect is None:
        from crawler_io import inspect_content as inspect
    in_a_row, done = 0, 0
    try:
        for i, link in enumerate(todo, 1):
            r = fetcher.fetch(link["url"])
            ins = None
            if not (r.error or r.blocked or not r.status or r.status >= 400):
                ins = inspect(r.content, r.content_type, r.final_url or link["url"], payer_alias)
            v = judge(link, r, ins)
            if v.verdict == "unreachable" and is_network_error(v.reason):
                in_a_row += 1
                log(f"  [{i}/{len(todo)}] no connection ({v.reason}) - not saved, will retry {link['url'][:90]}")
                if in_a_row >= STOP_AFTER_NETWORK_ERRORS:
                    raise NetworkDown(f"{in_a_row} links in a row could not connect ({v.reason}). This machine "
                                      "cannot reach the site right now - check the internet/DNS/proxy in this "
                                      "terminal, then run verify again (nothing was lost).")
                continue
            in_a_row = 0
            store.put(v)
            done += 1
            log(f"  [{i}/{len(todo)}] {v.verdict:<11} {link['url'][:110]}")
    finally:
        if own:
            fetcher.close()
    return done
