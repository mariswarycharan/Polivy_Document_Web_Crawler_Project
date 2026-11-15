"""Phase 3c: settles "ambiguous" verdicts with the crawler's own local AI (Ollama), the way the crawler does.

The crawler's LLMClassifier (crawler_state.py) is reused unchanged; only its answer cache is ours
(output/llm_cache_search_engine.jsonl), so the crawler's database is never written. The decision after the AI
answer mirrors the crawler's Gate 3 (payer_policy_crawler.py):

  * AI says policy document with confidence >= 0.5            -> accepted
  * AI says no / unsure, but the rule scores are strong:
      link score >= 6 and content score >= 1, or
      content score >= 4 and link score >= 2, or
      link score >= 5, content score >= 2 and a document-repository path   -> accepted (rule override)
  * otherwise                                                    -> rejected

Rows the AI settled keep the crawler-rule verdict in 'pre_ai_verdict' and the AI answer in 'ai_*' fields.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import OUTPUT_DIR

AI_ACCEPT_MIN_CONFIDENCE = 0.5          # same threshold as the crawler


class JsonCache:
    """Stand-in for the crawler's StateDB: only the two methods LLMClassifier uses (llm_get / llm_put)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                        self.data[row["k"]] = row["v"]
                    except (ValueError, KeyError):
                        continue

    def llm_get(self, key: str):
        return self.data.get(key)

    def llm_put(self, key: str, value: dict) -> None:
        with self._lock:
            self.data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n")


def make_classifier(out_dir: Path = OUTPUT_DIR):
    """The crawler's LLMClassifier with our cache. Returns (classifier, message); classifier is None when
    USE_LLM is off in crawler_config.py, Ollama is not running, or the model is not pulled."""
    try:
        from crawler_config import OLLAMA_MODEL, OLLAMA_URL, USE_LLM
        from crawler_state import LLMClassifier
    except Exception as e:                                   # pragma: no cover - import problem in the crawler
        return None, f"local AI not available ({type(e).__name__}: {e})"
    if not USE_LLM:
        return None, "local AI is switched off (USE_LLM = False in crawler_config.py)"
    clf = LLMClassifier(JsonCache(Path(out_dir) / "llm_cache_search_engine.jsonl"))
    if not clf.enabled:
        return None, (f"local AI not ready - start Ollama (`ollama serve`) and check `ollama list` shows "
                      f"{OLLAMA_MODEL} ({OLLAMA_URL})")
    return clf, f"local AI ready: {OLLAMA_MODEL}"


def ai_decide(ai: dict | None, ls: float, cs: float, link_signals: list[str]) -> tuple[str, float, str]:
    """Pure decision step (no network): the crawler's Gate 3 after the AI answered. Returns (verdict, confidence, reason)."""
    import re
    from crawler_config import OLLAMA_MODEL
    from .config import AI_SAYS_NO_PATTERNS
    if ai is not None and ai.get("is_policy_document") and re.search(AI_SAYS_NO_PATTERNS, ai.get("reason", ""), re.I):
        ai = dict(ai, is_policy_document=False)          # its own words say no: trust the words
    if ai is None:                                           # AI failed on this one: crawler's no-LLM fallback
        if cs >= 3 or ls >= 5:
            return "accepted", 0.50, "mixed rule evidence, accepted without AI confirmation (AI gave no answer)"
        return "rejected", 0.0, "mixed evidence, too weak without AI confirmation (AI gave no answer)"
    if ai.get("is_policy_document") and float(ai.get("confidence", 0)) >= AI_ACCEPT_MIN_CONFIDENCE:
        conf = min(0.75, 0.50 + 0.25 * float(ai.get("confidence", 0)))
        return "accepted", round(conf, 2), f"local AI ({OLLAMA_MODEL}) confirmed: {ai.get('reason', '')}"
    if ls >= 6 and cs >= 1:
        why = "strong link context (ls>=6) with some content markers"
    elif cs >= 4 and ls >= 2:
        why = "strong content fingerprint (cs>=4) with valid link context"
    elif ls >= 5 and cs >= 2 and "repo_path" in link_signals:
        why = "document repository path + moderate scores"
    else:
        return "rejected", 0.0, (f"local AI ({OLLAMA_MODEL}) rejected: {ai.get('reason', '')}; "
                                 f"rule scores insufficient (ls={ls}, cs={cs})")
    return "accepted", 0.60, f"local AI rejected/unsure, but {why}"


def redecide(row: dict) -> tuple[str, str]:
    """Gate 3 again from a stored row's saved AI answer (no network, no AI call), so changes to the decision
    rules apply to old answers too. Returns (verdict, reason)."""
    from crawler_text import score_link
    ai = None
    if row.get("ai_verdict") in ("policy", "not policy"):
        ai = {"is_policy_document": row["ai_verdict"] == "policy", "confidence": row.get("ai_confidence", 0),
              "reason": row.get("ai_reason", "")}
    _, sigs = score_link(row["url"], anchor=row.get("title", ""))
    verdict, _conf, why = ai_decide(ai, row.get("link_score", 0) or 0, row.get("content_score", 0) or 0, sigs)
    return verdict, why


def describe_file_name(url: str) -> str:
    """The link's file name in words, with payer abbreviations spelled out:
    'MDWP_CAID_AUG24CUMG.pdf' -> 'file name: MDWP_CAID_AUG24CUMG.pdf (mdwp caid aug24 cumg pdf; caid = medicaid,
    cumg = clinical utilization management guidelines)'."""
    import re
    from urllib.parse import unquote, urlparse
    from crawler_text import url_words
    from .config import AI_FILENAME_ABBREVIATIONS
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or url
    words = url_words("/" + name)
    tokens = set(re.findall(r"[a-z]+", words))                 # 'aug24' 'cumg' -> 'aug', 'cumg'
    found = [f"{k} = {v}" for k, v in AI_FILENAME_ABBREVIATIONS.items() if k in tokens]
    return f"file name: {name} ({words}" + (f"; {', '.join(found)}" if found else "") + ")"


def settle(row: dict, ins, payer_alias: str, classifier) -> dict:
    """Ask the AI about one ambiguous row (with its fetched content `ins`) and return the updated stored row."""
    from crawler_text import content_fingerprint, score_link
    from .config import AI_PROMPT_VERSION
    title = row.get("title", "")
    ls, sigs = score_link(row["url"], anchor=title, is_page=(ins.file_type == "html"))
    cs, _ = content_fingerprint(ins.text)
    title_guess = ins.heading_title or (ins.titles[0] if getattr(ins, "titles", None) else "") or title
    link_text = title or describe_file_name(row["url"])          # never an empty link text
    context = describe_file_name(row["url"])
    ai = classifier.classify(payer_alias, row.get("final_url") or row["url"], link_text, context,
                             title_guess or link_text, ins.text, is_disambiguation=True)
    verdict, conf, why = ai_decide(ai, ls, cs, sigs)
    out = dict(row)
    first = "pre_ai_verdict" not in row                          # a re-ask keeps the original rule verdict
    out.update(pre_ai_verdict=row.get("crawler_verdict", row.get("verdict")) if first else row["pre_ai_verdict"],
               pre_ai_reason=row.get("crawler_reason", row.get("reason")) if first else row.get("pre_ai_reason", ""),
               ai_version=AI_PROMPT_VERSION,
               verdict=verdict, reason=why, confidence=conf,
               ai_verdict=("policy" if ai and ai.get("is_policy_document") else "not policy" if ai else "no answer"),
               ai_confidence=float(ai.get("confidence", 0)) if ai else 0.0,
               ai_reason=(ai or {}).get("reason", ""),
               ai_document_type=(ai or {}).get("document_type", ""),
               ai_line_of_business=(ai or {}).get("line_of_business", ""))
    for k in ("crawler_verdict", "crawler_reason"):          # strict_review recomputes these from the new verdict
        out.pop(k, None)
    if ai and ai.get("document_type") and row.get("document_type") in ("", "other"):
        out["document_type"] = ai["document_type"]      # also lets EXCLUDED_DOC_TYPES apply to AI answers
    return out


def language_base(url: str) -> str:
    """File name without its language suffix: in_x_eng.pdf / in_x_spa.pdf -> in_x.pdf (same document)."""
    import re
    from urllib.parse import urlparse
    from .config import LANGUAGE_SUFFIXES
    p = urlparse(url)
    name = p.path.rsplit("/", 1)[-1].lower()
    suf = "|".join(sorted(LANGUAGE_SUFFIXES, key=len, reverse=True))
    base = re.sub(rf"[_-](?:{suf})(?=\.[a-z0-9]+$|$)", "", name)
    return f"{p.netloc.lower()}{p.path.rsplit('/', 1)[0].lower()}/{base}"


def copy_sibling(row: dict, store) -> dict | None:
    """If another language version of this document was already decided by the AI (current prompt), reuse it."""
    from .config import AI_PROMPT_VERSION
    base = language_base(row["url"])
    for other in store.rows.values():
        if other["url_key"] != row["url_key"] and other.get("ai_verdict") and \
                other.get("ai_version", 1) >= AI_PROMPT_VERSION and language_base(other["url"]) == base:
            out = dict(row)
            first = "pre_ai_verdict" not in row
            out.update({k: other.get(k) for k in ("ai_verdict", "ai_confidence", "ai_reason", "ai_document_type",
                                                  "ai_line_of_business", "ai_version", "confidence")})
            out.update(pre_ai_verdict=row.get("crawler_verdict", row.get("verdict")) if first else row["pre_ai_verdict"],
                       pre_ai_reason=row.get("crawler_reason", row.get("reason")) if first else row.get("pre_ai_reason", ""),
                       verdict=other["verdict"], ai_from=other["url"],
                       reason=f"same document in another language: {other['url'].rsplit('/', 1)[-1][:60]}",
                       document_type=other.get("document_type", row.get("document_type", "")))
            out.pop("crawler_verdict", None)
            out.pop("crawler_reason", None)
            return out
    return None


def needs_ai(row: dict) -> bool:
    """Ambiguous and not yet seen by the AI, or settled by the AI with an older prompt version."""
    from .config import AI_PROMPT_VERSION
    if row.get("ai_verdict"):
        return row.get("ai_version", 1) < AI_PROMPT_VERSION
    return row.get("verdict") == "ambiguous"


def review_ambiguous(rows: list[dict], payer_alias: str, store, classifier, fetcher=None, inspect=None,
                     max_links: int | None = None, log=print) -> int:
    """Re-fetch each ambiguous row not yet seen by the AI, ask the AI, save the settled verdict. Returns count."""
    todo = [r for r in rows if needs_ai(r)]
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
    done = 0
    try:
        for i, row in enumerate(todo, 1):
            twin = copy_sibling(row, store)
            if twin is not None:                         # other language version already decided: no fetch, no AI
                store.put_row(twin)
                done += 1
                log(f"  [{i}/{len(todo)}] {store.rows[row['url_key']]['verdict']:<9} same as other language   {row['url'][:80]}")
                continue
            r = fetcher.fetch(row["url"])
            if r.error or r.blocked or not r.status or r.status >= 400:
                log(f"  [{i}/{len(todo)}] skipped (could not fetch again: {r.blocked or r.error or r.status}) {row['url'][:90]}")
                continue
            ins = inspect(r.content, r.content_type, r.final_url or row["url"], payer_alias)
            new = settle(row, ins, payer_alias, classifier)
            store.put_row(new)
            done += 1
            final = store.rows[row["url_key"]]
            log(f"  [{i}/{len(todo)}] {final['verdict']:<9} AI: {final.get('ai_verdict', ''):<10} {row['url'][:90]}")
    finally:
        if own:
            fetcher.close()
    return done
