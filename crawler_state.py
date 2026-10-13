#!/usr/bin/env python3
"""Module 4/5 - state & models: local LLM (Gate 3), SQLite checkpoint, PolicyRow schema model and schema-dictionary loader.

Part of the Payer Policy Document Discovery Crawler (v2). Logic is unchanged from the single-file version."""
from __future__ import annotations

import httpx
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pydantic import BaseModel, field_validator, model_validator
from typing import Any, Optional

from crawler_config import (COLUMNS, ENUMS, LLM_MAX_ATTEMPTS, LLM_NUM_CTX, LLM_NUM_PREDICT, LLM_TIMEOUT_SECONDS, MAX_LLM_CALLS_PER_PAYER, OLLAMA_MODEL, OLLAMA_URL, REOPEN_REJECTED_SCORE_GAIN, SCHEMA_DICTIONARY_XLSX, STATE_JOINER, STATE_NATIONAL, USE_LLM, log, utc_now_iso)
from crawler_text import (USPS_CODES, clean_text, sha256_hex, url_key)


# =============================================================================
# 12. LLM (Gate 3) - Ollama + Qwen2.5, ambiguous cases only, with a JSON repair parser
# =============================================================================
LLM_SYSTEM_PROMPT = """You classify ONE document found on a US health-insurance (payer) website.

TASK
Decide whether the document is a publicly published, provider-facing POLICY DOCUMENT. If it is, give its type and line of business.

DOCUMENT TYPES - document_type must be exactly one of these strings:
- medical_policy: medical, coverage or clinical policy saying when a service, procedure, device or test is covered (medical necessity, coverage rationale, CPT/HCPCS codes).
- pharmacy_policy: pharmacy or medical-benefit drug policy with coverage criteria for specific drugs (including step therapy and quantity-limit criteria).
- coverage_guideline: clinical, utilization-management, medical-necessity or coverage-determination guideline or criteria set.
- pa_form: form a provider fills in to request prior authorization, precertification, a formulary exception or a coverage determination.
- pa_list: list of services, codes or drugs that require prior authorization or precertification.
- formulary: a formulary (drugs with tiers or coverage status).
- drug_list: a drug list, preferred drug list (PDL) or specialty drug list.
- provider_manual: provider manual, provider handbook or administrative guide.
- bulletin: provider bulletin, policy update or notice of policy changes.
- other: a real policy document that fits none of the above, for example a reimbursement or payment policy.

NOT POLICY DOCUMENTS (is_policy_document = false):
privacy notice, terms of use, nondiscrimination or language-assistance notice, member handbook, evidence of coverage, summary of benefits, plan brochure, flyer, news or press release, provider directory, claim form unrelated to authorization, job posting, financial report, login page, and index pages that only list links to other documents.

LINE OF BUSINESS - line_of_business must be exactly one of:
Commercial (employer / group plans), Medicare (Medicare Advantage, Part D, D-SNP), Medicaid (Medicaid, CHIP, state programs), Exchange (ACA marketplace, individual and family plans), Federal (Federal Employee Program / FEP), Multiple (the text itself names more than one), Unknown (not stated).
Use only words in the evidence. Never infer it from the payer name. A citation of Medicare rules inside a commercial policy does not make it Medicare.

RULES
1. Use only the evidence given. Do not invent facts.
2. The URL alone is weak evidence; the title and text are strong evidence.
3. If the evidence is weak or contradictory, answer is_policy_document false with confidence 0.4 or lower.
4. confidence is your probability (0.0 to 1.0) that the whole answer is correct.

OUTPUT FORMAT - FOLLOW EXACTLY
- Reply with ONE JSON object and NOTHING else: no markdown, no code fences, no words before or after it.
- Exactly these five keys, in this order: is_policy_document, document_type, line_of_business, confidence, reason.
- Use double quotes for every key and every string value.
- is_policy_document is true or false (lowercase, no quotes). confidence is a number such as 0.85 (no quotes).
- reason is at most 12 words on one line and must not contain double quotes, backslashes or line breaks.
- End the reply with the closing brace }.
Example of a valid reply:
{"is_policy_document": true, "document_type": "medical_policy", "line_of_business": "Commercial", "confidence": 0.9, "reason": "Commercial Medical Policy with coverage rationale and CPT codes"}"""

LLM_RETRY_PROMPT = ("Your previous reply was not one valid JSON object. Reply again with ONLY the JSON object, "
                    "double-quoted keys and strings, the five keys in order, a reason of at most 12 words, "
                    "and nothing before or after the braces.")

LLM_FEWSHOT = [
    ("PAYER: Example Health\nURL: https://www.example.com/docs/48271\nLINK TEXT: Bariatric Surgery\n"
     "PAGE CONTEXT: Commercial Medical Policies\nTITLE: Bariatric Surgery\nTEXT:\nCommercial Medical Policy  Policy Number: 2025T0362Q  "
     "Effective Date: January 1, 2025. Coverage Rationale: Bariatric surgery is proven and medically necessary when ... CPT 43644 43645",
     {"is_policy_document": True, "document_type": "medical_policy", "line_of_business": "Commercial", "confidence": 0.95,
      "reason": "Commercial Medical Policy with coverage rationale and CPT codes"}),
    ("PAYER: Example Health\nURL: https://www.example.com/files/np-2024.pdf\nLINK TEXT: Notice\nPAGE CONTEXT: Members\n"
     "TITLE: Notice of Privacy Practices\nTEXT:\nThis notice describes how medical information about you may be used and disclosed ...",
     {"is_policy_document": False, "document_type": "other", "line_of_business": "Unknown", "confidence": 0.95,
      "reason": "privacy notice, not a provider policy"}),
    ("PAYER: Example Health\nURL: https://www.example.com/rx/pa-req.pdf\nLINK TEXT: Humira request\nPAGE CONTEXT: Pharmacy forms\n"
     "TITLE: Prior Authorization Request\nTEXT:\nMedicaid. Member name, Member ID, Prescriber NPI, Diagnosis, Fax completed form to ...",
     {"is_policy_document": True, "document_type": "pa_form", "line_of_business": "Medicaid", "confidence": 0.9,
      "reason": "Medicaid prior authorization request form with member and prescriber fields"}),
]

_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"',
                               "\u2018": "'", "\u2019": "'", "\u201a": "'"})
_FENCE_RX = re.compile(r"```[a-zA-Z]*\s*|```")


def _balanced_object(s: str) -> str:
    """Text from the first '{' to its matching '}' (string- and escape-aware). Unterminated -> rest of the text."""
    start = s.find("{")
    if start < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def _close_truncated(s: str) -> str:
    """Repair output cut off mid-way: close an open string, drop a dangling key / comma, close brackets."""
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s = (s[:-1] if s.endswith("\\") else s) + '"'
    s = s.rstrip()
    s = re.sub(r',\s*"[^"]*"\s*:?\s*$', "", s)      # dangling key (with or without colon) after a comma
    s = re.sub(r":\s*$", ": null", s)
    s = re.sub(r",\s*$", "", s)
    return s + "".join(reversed(stack))


def _single_to_double(s: str) -> str:
    """{'a': 'b'} -> {"a": "b"} without touching apostrophes inside words (payer's)."""
    s = re.sub(r"(?<=[{,\[:])\s*'([^'\\]*(?:\\.[^'\\]*)*)'(?=\s*[:,}\]])", lambda m: ' "' + m.group(1).replace('"', "") + '"', s)
    return s


def _regex_fields(s: str) -> Optional[dict]:
    m = re.search(r'is_policy_document["\']?\s*[:=]\s*["\']?(true|false|yes|no|1|0)\b', s, re.I)
    if not m:
        return None
    d: dict[str, Any] = {"is_policy_document": m.group(1).lower() in ("true", "yes", "1")}
    for k in ("document_type", "line_of_business"):
        mm = re.search(rf'{k}["\']?\s*[:=]\s*["\']?([A-Za-z_ /\-]+)', s, re.I)
        if mm:
            d[k] = mm.group(1).strip()
    mm = re.search(r'confidence["\']?\s*[:=]\s*["\']?([0-9]*\.?[0-9]+)', s, re.I)
    if mm:
        d["confidence"] = mm.group(1)
    mm = re.search(r'reason["\']?\s*[:=]\s*["\']?([^"\'\n}]*)', s, re.I)
    if mm:
        d["reason"] = mm.group(1)
    return d


def parse_llm_json(raw: Any) -> tuple[Optional[dict], str]:
    """Parse a small model's reply into a dict no matter how it is wrapped or damaged.
    Handles: code fences, chatter before/after, smart quotes, Python literals, single quotes, unquoted keys,
    trailing commas, missing commas, raw newlines in strings, bad escapes, truncated output; finally a per-field
    regex fallback. Returns (dict or None, how) with how in ok | repaired | regex_fallback | empty | unparseable."""
    if isinstance(raw, dict):
        return raw, "ok"
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty"
    s = raw.strip().lstrip("\ufeff").translate(_SMART_QUOTES)
    s = _FENCE_RX.sub("", s).strip()
    cand = _balanced_object(s)
    tries: list[str] = []
    if cand:
        tries.append(cand)
        f = re.sub(r"[\r\n\t]+", " ", cand)
        f = re.sub(r"(?<![\"\w])True(?![\"\w])", "true", f)
        f = re.sub(r"(?<![\"\w])False(?![\"\w])", "false", f)
        f = re.sub(r"(?<![\"\w])None(?![\"\w])", "null", f)
        tries.append(f)
        if "'" in f and f.count('"') < 4:
            f = _single_to_double(f)
        f = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', f)          # unquoted keys
        f = re.sub(r",\s*([}\]])", r"\1", f)                                          # trailing commas
        f = re.sub(r'("|\d|true|false|null)\s+("[A-Za-z_]+"\s*:)', r"\1, \2", f)     # missing commas between pairs
        f = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", f)                                 # invalid escapes
        tries.append(f)
        tries.append(_close_truncated(f))
    for i, t in enumerate(tries):
        try:
            obj = json.loads(t, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            obj = obj[0]
        if isinstance(obj, dict):
            return obj, ("ok" if i == 0 else "repaired")
    obj = _regex_fields(s)
    if obj:
        return obj, "regex_fallback"
    return None, "unparseable"


_DT_SYNONYMS = {
    "medicalpolicy": "medical_policy", "coveragepolicy": "medical_policy", "clinicalpolicy": "medical_policy",
    "medicalcoveragepolicy": "medical_policy", "pharmacypolicy": "pharmacy_policy", "drugpolicy": "pharmacy_policy",
    "pharmacyclinicalpolicy": "pharmacy_policy", "coverageguideline": "coverage_guideline",
    "clinicalguideline": "coverage_guideline", "priorauthcriteria": "coverage_guideline",
    "priorauthorizationcriteria": "coverage_guideline", "medicalnecessityguideline": "coverage_guideline",
    "paform": "pa_form", "priorauthform": "pa_form", "priorauthorizationform": "pa_form", "palist": "pa_list",
    "priorauthlist": "pa_list", "priorauthorizationlist": "pa_list", "formulary": "formulary", "druglist": "drug_list",
    "preferreddruglist": "drug_list", "pdl": "drug_list", "providermanual": "provider_manual", "bulletin": "bulletin",
    "policyupdate": "bulletin", "reimbursementpolicy": "other", "paymentpolicy": "other", "other": "other",
}
_LOB_SYNONYMS = {
    "commercial": "Commercial", "employer": "Commercial", "group": "Commercial", "medicare": "Medicare",
    "medicareadvantage": "Medicare", "partd": "Medicare", "medicaid": "Medicaid", "chip": "Medicaid",
    "exchange": "Exchange", "marketplace": "Exchange", "aca": "Exchange", "individual": "Exchange",
    "federal": "Federal", "fep": "Federal", "multiple": "Multiple", "unknown": "Unknown",
}


def normalize_llm_result(d: Any) -> Optional[dict]:
    if not isinstance(d, dict) or "is_policy_document" not in d:
        return None
    ok = d.get("is_policy_document")
    if isinstance(ok, str):
        ok = ok.strip().lower() in ("true", "yes", "y", "1")
    nk = lambda s: re.sub(r"[^a-z]", "", str(s or "").lower())
    dt_raw = str(d.get("document_type", "other")).strip()
    dt = dt_raw if dt_raw in ENUMS["document_type"] else _DT_SYNONYMS.get(nk(dt_raw), "other")
    lob_raw = str(d.get("line_of_business", "Unknown")).strip()
    lob = lob_raw if lob_raw in ENUMS["line_of_business"] else _LOB_SYNONYMS.get(nk(lob_raw), "Unknown")
    try:
        conf = float(str(d.get("confidence", 0.5)).strip().rstrip("%"))
    except (TypeError, ValueError):
        conf = 0.5
    if 1.0 < conf <= 100.0:
        conf /= 100.0
    return {"is_policy_document": bool(ok), "document_type": dt, "line_of_business": lob,
            "confidence": max(0.0, min(1.0, conf)), "reason": clean_text(str(d.get("reason", "")).replace('"', "").replace("\\", ""), 160)}


class LLMClassifier:
    def __init__(self, db: "StateDB") -> None:
        self.db = db
        self.enabled = USE_LLM
        self.calls = 0
        self.failures = 0
        self.http = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)
        if self.enabled:
            try:
                tags = self.http.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
                names = {m.get("name", "") for m in tags.get("models", [])} | {m.get("model", "") for m in tags.get("models", [])}
                if not any(n == OLLAMA_MODEL or n == f"{OLLAMA_MODEL}:latest" or n.startswith(OLLAMA_MODEL + "-") for n in names):
                    log(logging.WARNING, "llm_model_missing", model=OLLAMA_MODEL, available=sorted(names)[:10],
                        hint=f"ollama pull {OLLAMA_MODEL}")
                    self.enabled = False
                else:
                    log(logging.INFO, "llm_ready", model=OLLAMA_MODEL)
            except Exception as e:
                log(logging.WARNING, "llm_unavailable", error=str(e)[:150], hint="start `ollama serve`; LLM gate disabled")
                self.enabled = False

    def _schema(self) -> dict:
        return {"type": "object", "properties": {
            "is_policy_document": {"type": "boolean"},
            "document_type": {"type": "string", "enum": ENUMS["document_type"]},
            "line_of_business": {"type": "string", "enum": ENUMS["line_of_business"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 120}},
            "required": ["is_policy_document", "document_type", "line_of_business", "confidence", "reason"]}

    def classify(self, payer: str, url: str, anchor: str, context: str, title: str, text: str) -> Optional[dict]:
        if not self.enabled or self.calls >= MAX_LLM_CALLS_PER_PAYER:
            return None
        evidence = (f"PAYER: {payer}\nURL: {url}\nLINK TEXT: {clean_text(anchor, 200)}\nPAGE CONTEXT: {clean_text(context, 300)}\n"
                    f"TITLE: {clean_text(title, 250)}\nTEXT:\n{clean_text(text, 1500)}")
        key = sha256_hex((OLLAMA_MODEL + LLM_SYSTEM_PROMPT + evidence).encode())
        cached = self.db.llm_get(key)
        if cached is not None:
            return cached
        base = [{"role": "system", "content": LLM_SYSTEM_PROMPT}]
        for u, a in LLM_FEWSHOT:
            base += [{"role": "user", "content": u}, {"role": "assistant", "content": json.dumps(a)}]
        base.append({"role": "user", "content": evidence})
        modes = [("schema", self._schema()), ("json", "json"), ("free", None)][:max(1, LLM_MAX_ATTEMPTS)]
        result, last_raw = None, ""
        for attempt, (mode, fmt) in enumerate(modes, start=1):
            if self.calls >= MAX_LLM_CALLS_PER_PAYER:
                break
            msgs = list(base)
            if attempt > 1 and last_raw:
                msgs += [{"role": "assistant", "content": last_raw[:600]}, {"role": "user", "content": LLM_RETRY_PROMPT}]
            payload: dict[str, Any] = {"model": OLLAMA_MODEL, "messages": msgs, "stream": False, "keep_alive": "15m",
                                       "options": {"temperature": 0, "top_p": 0.9, "num_ctx": LLM_NUM_CTX,
                                                   "num_predict": LLM_NUM_PREDICT, "seed": 7}}
            if fmt is not None:
                payload["format"] = fmt
            try:
                self.calls += 1
                r = self.http.post(f"{OLLAMA_URL}/api/chat", json=payload)
                r.raise_for_status()
                last_raw = ((r.json() or {}).get("message") or {}).get("content", "") or ""
            except httpx.ConnectError as e:
                log(logging.WARNING, "llm_connection_lost", error=str(e)[:150])
                self.enabled = False
                break
            except Exception as e:
                log(logging.DEBUG, "llm_http_error", attempt=attempt, mode=mode, error=str(e)[:200])
                continue
            obj, how = parse_llm_json(last_raw)
            result = normalize_llm_result(obj) if obj else None
            if result:
                result["parse"] = how
                if how != "ok":
                    log(logging.DEBUG, "llm_json_repaired", attempt=attempt, mode=mode, how=how, raw=last_raw[:300])
                break
            log(logging.DEBUG, "llm_unparseable", attempt=attempt, mode=mode, how=how, raw=last_raw[:300])
        if result:
            self.db.llm_put(key, result)
        else:
            self.failures += 1
            log(logging.DEBUG, "llm_failed", url=url, raw=last_raw[:300])
        return result


# =============================================================================
# 13. STATE / CHECKPOINT (SQLite)
# =============================================================================
class StateDB:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;      -- WAL + NORMAL: no fsync per commit, still crash-safe for the checkpoint
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-65536;
        PRAGMA mmap_size=268435456;
        PRAGMA busy_timeout=30000;
        CREATE TABLE IF NOT EXISTS frontier(
            payer TEXT, key TEXT, url TEXT, kind TEXT, priority REAL, depth INT, budget INT,
            source_page_url TEXT, discovery_path TEXT, anchor TEXT, heading TEXT, page_ctx TEXT,
            hints TEXT, link_score REAL, signals TEXT, render_mode TEXT, route TEXT,
            status TEXT DEFAULT 'pending', reason TEXT, updated_at TEXT, PRIMARY KEY(payer, key));
        CREATE INDEX IF NOT EXISTS ix_frontier ON frontier(payer, status, priority);
        CREATE INDEX IF NOT EXISTS ix_frontier_kind ON frontier(payer, kind, status);
        CREATE INDEX IF NOT EXISTS ix_frontier_pick ON frontier(payer, kind, status, priority DESC, depth);
        CREATE TABLE IF NOT EXISTS documents(
            payer TEXT, key TEXT, document_url TEXT, accepted INT, row_json TEXT, content_hash TEXT,
            decision TEXT, route TEXT, created_at TEXT, PRIMARY KEY(payer, key));
        CREATE TABLE IF NOT EXISTS skipped_links(
            payer TEXT, key TEXT, url TEXT, source_page_url TEXT, anchor TEXT, score REAL, hub_score REAL,
            reason TEXT, at TEXT, PRIMARY KEY(payer, key));
        CREATE TABLE IF NOT EXISTS phases(payer TEXT, phase TEXT, done_at TEXT, PRIMARY KEY(payer, phase));
        CREATE TABLE IF NOT EXISTS kv(payer TEXT, k TEXT, v TEXT, PRIMARY KEY(payer, k));
        CREATE TABLE IF NOT EXISTS blocked_pages(payer TEXT, url TEXT, reason TEXT, status INT, at TEXT, PRIMARY KEY(payer, url));
        CREATE TABLE IF NOT EXISTS llm_cache(k TEXT PRIMARY KEY, v TEXT);
        """)
        self.con.commit()

    def reset_payer(self, payer: str) -> None:
        for t in ("frontier", "documents", "phases", "kv", "blocked_pages", "skipped_links"):
            self.con.execute(f"DELETE FROM {t} WHERE payer=?", (payer,))
        self.con.commit()

    def recover_in_progress(self, payer: str) -> int:
        n = self.con.execute("UPDATE frontier SET status='pending' WHERE payer=? AND status='in_progress'", (payer,)).rowcount
        self.con.commit()
        return n

    def payers(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT payer FROM frontier ORDER BY payer")]

    # phases / kv
    def phase_done(self, payer: str, phase: str) -> bool:
        return self.con.execute("SELECT 1 FROM phases WHERE payer=? AND phase=?", (payer, phase)).fetchone() is not None

    def mark_phase(self, payer: str, phase: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO phases VALUES(?,?,?)", (payer, phase, utc_now_iso()))
        self.con.commit()

    def get(self, payer: str, k: str, default: Any = None) -> Any:
        r = self.con.execute("SELECT v FROM kv WHERE payer=? AND k=?", (payer, k)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, payer: str, k: str, v: Any) -> None:
        self.con.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?)", (payer, k, json.dumps(v)))
        self.con.commit()

    # frontier
    def enqueue(self, payer: str, item: dict) -> bool:
        key = url_key(item["url"])
        cur = self.con.execute("SELECT status, link_score FROM frontier WHERE payer=? AND key=?", (payer, key)).fetchone()
        vals = (item["priority"], item["source_page_url"], item["discovery_path"], item.get("anchor", ""), item.get("heading", ""),
                item.get("page_ctx", ""), json.dumps(item.get("hints", {})), item["link_score"],
                json.dumps(item.get("signals", [])), item.get("render_mode", "static"), item.get("route", ""), item["kind"],
                item["depth"], item.get("budget", 99))
        if cur is None:
            self.con.execute("""INSERT INTO frontier(payer,key,url,kind,priority,depth,budget,source_page_url,discovery_path,anchor,heading,
                page_ctx,hints,link_score,signals,render_mode,route,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                             (payer, key, item["url"], item["kind"], item["priority"], item["depth"], item.get("budget", 99),
                              item["source_page_url"], item["discovery_path"], item.get("anchor", ""), item.get("heading", ""),
                              item.get("page_ctx", ""), json.dumps(item.get("hints", {})), item["link_score"],
                              json.dumps(item.get("signals", [])), item.get("render_mode", "static"), item.get("route", ""),
                              utc_now_iso()))
            self.con.execute("DELETE FROM skipped_links WHERE payer=? AND key=?", (payer, key))
            return True
        old = float(cur["link_score"] or 0)
        upd = """UPDATE frontier SET priority=?, source_page_url=?, discovery_path=?, anchor=?, heading=?, page_ctx=?, hints=?,
                 link_score=?, signals=?, render_mode=?, route=?, kind=?, depth=?, budget=?"""
        if cur["status"] == "pending" and item["link_score"] > old:
            self.con.execute(upd + " WHERE payer=? AND key=?", (*vals, payer, key))
            return False
        # a rejected document is re-evaluated when it re-appears with clearly stronger evidence
        if cur["status"] == "rejected" and item["kind"] in ("doc", "maybe_doc") and \
                item["link_score"] >= old + REOPEN_REJECTED_SCORE_GAIN:
            self.con.execute(upd + ", status='pending', reason='reopened: stronger link context', updated_at=? WHERE payer=? AND key=?",
                             (*vals, utc_now_iso(), payer, key))
            log(logging.DEBUG, "candidate_reopened", payer=payer, url=item["url"], old_score=old, new_score=item["link_score"])
            return True
        return False

    def skip_link(self, payer: str, url: str, source: str, anchor: str, score: float, hub: float, reason: str) -> None:
        self.con.execute("INSERT OR IGNORE INTO skipped_links VALUES(?,?,?,?,?,?,?,?,?)",
                         (payer, url_key(url), url, source, clean_text(anchor, 200), score, hub, reason, utc_now_iso()))

    def commit(self) -> None:
        self.con.commit()

    def _top_pending(self, payer: str, kinds: tuple[str, ...], n: int) -> list[sqlite3.Row]:
        """Best `n` pending items over `kinds` (priority DESC, depth ASC). One indexed lookup per kind, merged in Python,
        so a long queue of the other kind is never scanned."""
        rows: list[sqlite3.Row] = []
        for k in kinds:
            rows += self.con.execute(
                "SELECT * FROM frontier WHERE payer=? AND status='pending' AND kind=? "
                "ORDER BY priority DESC, depth ASC LIMIT ?", (payer, k, n)).fetchall()
        rows.sort(key=lambda r: (-float(r["priority"] or 0), int(r["depth"] or 0)))
        return rows[:n]

    def next_item(self, payer: str, kinds: tuple[str, ...]) -> Optional[dict]:
        top = self._top_pending(payer, kinds, 1)
        if top:
            row = top[0]
            self.con.execute("UPDATE frontier SET status='in_progress', updated_at=? WHERE payer=? AND key=?",
                             (utc_now_iso(), payer, row["key"]))
            self.con.commit()
            return dict(row)
        return None

    def peek_doc_urls(self, payer: str, n: int) -> list[str]:
        """URLs of the next `n` pending documents (the crawl loop always takes documents before pages) - not claimed."""
        return [r["url"] for r in self._top_pending(payer, ("doc", "maybe_doc"), n)]

    def finish(self, payer: str, key: str, status: str, reason: str = "") -> None:
        self.con.execute("UPDATE frontier SET status=?, reason=?, updated_at=? WHERE payer=? AND key=?",
                         (status, reason[:500], utc_now_iso(), payer, key))
        self.con.commit()

    def status_counts(self, payer: str) -> dict[tuple[str, str], int]:
        return {(r[0], r[1]): r[2] for r in self.con.execute(
            "SELECT kind, status, COUNT(*) FROM frontier WHERE payer=? GROUP BY kind, status", (payer,))}

    # documents
    def doc_state(self, payer: str, key: str) -> Optional[sqlite3.Row]:
        return self.con.execute("SELECT accepted, decision FROM documents WHERE payer=? AND key=?", (payer, key)).fetchone()

    def doc_accepted(self, payer: str, key: str) -> bool:
        r = self.doc_state(payer, key)
        return bool(r and r["accepted"])

    def save_doc(self, payer: str, key: str, url: str, accepted: bool, row: dict, decision: dict, route: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                         (payer, key, url, int(accepted), json.dumps(row), row.get("content_hash_sha256", ""),
                          json.dumps(decision, default=str), route, utc_now_iso()))
        self.con.commit()

    def accepted_count(self, payer: str) -> int:
        return self.con.execute("SELECT COUNT(*) FROM documents WHERE payer=? AND accepted=1", (payer,)).fetchone()[0]

    def block_page(self, payer: str, url: str, reason: str, status: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO blocked_pages VALUES(?,?,?,?,?)", (payer, url, reason, status, utc_now_iso()))
        self.con.commit()

    # llm cache
    def llm_get(self, k: str) -> Optional[dict]:
        r = self.con.execute("SELECT v FROM llm_cache WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else None

    def llm_put(self, k: str, v: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO llm_cache VALUES(?,?)", (k, json.dumps(v)))
        self.con.commit()


# =============================================================================
# 14. ROW MODEL - every schema rule from schema_dictionary.xlsx enforced on every row
# =============================================================================
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_URL_OK = re.compile(r"^https?://[^\s/$.?#][^\s]*$")


class PolicyRow(BaseModel):
    payer_name: str
    payer_alias: str = ""
    state_or_region: str
    line_of_business: str
    document_title: str
    document_type: str
    document_url: str
    source_page_url: str = ""
    discovery_path: str
    file_type: str
    policy_number: str = ""
    effective_date: str = ""
    last_updated_date: str = ""
    http_status: int
    content_hash_sha256: str = ""
    file_size_bytes: int
    requires_auth: str
    render_mode: str
    extraction_method: str
    confidence_score: float
    scrape_timestamp_utc: str
    notes: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _no_null_words(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("none", "null", "n/a", "na", "nan", "-", "tbd")):
            return ""
        return v.strip() if isinstance(v, str) else v

    @field_validator("payer_name", "document_title", "discovery_path")
    @classmethod
    def _required_text(cls, v: str) -> str:
        if not v:
            raise ValueError("required field is empty")
        return v

    @field_validator("state_or_region")
    @classmethod
    def _state(cls, v: str) -> str:
        parts = v.split(STATE_JOINER)
        if not v or any(p not in USPS_CODES and p != STATE_NATIONAL for p in parts):
            raise ValueError(f"state_or_region must be USPS codes or National, got {v!r}")
        if STATE_NATIONAL in parts and len(parts) > 1:
            raise ValueError("National cannot be combined with state codes")
        if len(set(parts)) != len(parts):
            raise ValueError("duplicate state codes")
        return v

    @field_validator("document_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not _URL_OK.match(v):
            raise ValueError(f"invalid URL: {v!r}")
        return v

    @field_validator("source_page_url")
    @classmethod
    def _source_url(cls, v: str) -> str:
        if v and not _URL_OK.match(v):
            raise ValueError(f"invalid URL: {v!r}")
        return v

    @field_validator("effective_date", "last_updated_date")
    @classmethod
    def _date(cls, v: str) -> str:
        if v:
            if not _ISO_DATE.match(v):
                raise ValueError("date must be YYYY-MM-DD")
            datetime.strptime(v, "%Y-%m-%d")
        return v

    @field_validator("scrape_timestamp_utc")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not _ISO_TS.match(v):
            raise ValueError("timestamp must be UTC ISO-8601 with Z")
        return v

    @field_validator("content_hash_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        if v and not _HEX64.match(v):
            raise ValueError("hash must be lowercase hex sha256")
        return v

    @field_validator("http_status")
    @classmethod
    def _status(cls, v: int) -> int:
        v = int(v)
        if v != 0 and not 100 <= v <= 599:
            raise ValueError("http_status must be 0 (transport failure) or 100-599")
        return v

    @field_validator("file_size_bytes")
    @classmethod
    def _size(cls, v: int) -> int:
        if int(v) < 0:
            raise ValueError("negative size")
        return int(v)

    @field_validator("confidence_score")
    @classmethod
    def _conf(cls, v: float) -> float:
        v = round(float(v), 2)
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence out of range")
        return v

    @field_validator("line_of_business", "document_type", "file_type", "render_mode", "requires_auth")
    @classmethod
    def _enum(cls, v: str, info) -> str:
        if v not in ENUMS[info.field_name]:
            raise ValueError(f"{v!r} not in allowed values {ENUMS[info.field_name]}")
        return v

    @field_validator("extraction_method")
    @classmethod
    def _methods(cls, v: str) -> str:
        parts = v.split("|")
        if not v or any(p not in ENUMS["extraction_method"] for p in parts) or len(set(parts)) != len(parts):
            raise ValueError(f"extraction_method must be pipe-separated values of {ENUMS['extraction_method']}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> "PolicyRow":
        if self.requires_auth == "Y" and self.content_hash_sha256:
            raise ValueError("requires_auth=Y rows must have an empty content_hash_sha256")
        if self.http_status == 200 and self.requires_auth == "N" and not self.content_hash_sha256:
            raise ValueError("an http_status 200 row needs content_hash_sha256")
        if not self.content_hash_sha256 and self.file_size_bytes != 0:
            raise ValueError("file_size_bytes must be 0 when nothing was hashed")
        if self.confidence_score < 0.70 and not self.notes:
            raise ValueError("confidence below 0.70 must be explained in notes")
        if self.http_status != 200 and not self.notes:
            raise ValueError("non-200 rows must be explained in notes")
        if not self.source_page_url and not ({"sitemap", "url_pattern"} & set(self.extraction_method.split("|"))):
            raise ValueError("source_page_url may be empty only for sitemap / url_pattern discoveries")
        return self

    def as_output(self) -> dict[str, Any]:
        d = self.model_dump()
        d["confidence_score"] = f"{d['confidence_score']:.2f}"
        return {c: d[c] for c in COLUMNS}


def load_schema_dictionary() -> None:
    """Verify the 22 column names/order and take the allowed enum values from schema_dictionary.xlsx."""
    if not os.path.exists(SCHEMA_DICTIONARY_XLSX):
        log(logging.WARNING, "schema_dictionary_missing", path=SCHEMA_DICTIONARY_XLSX,
            hint="built-in copy of the dictionary rules is used; place schema_dictionary.xlsx next to the script")
        return
    try:
        import openpyxl
        wb = openpyxl.load_workbook(SCHEMA_DICTIONARY_XLSX, read_only=True, data_only=True)
    except Exception as e:
        log(logging.WARNING, "schema_dictionary_unreadable", error=str(e)[:200])
        return
    sheets = {ws.title.strip().lower(): ws for ws in wb.worksheets}
    rows_of = lambda ws: [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
    ws = sheets.get("schema")
    if ws is not None:
        rows = rows_of(ws)
        hdr = next((i for i, r in enumerate(rows[:10]) if any(c.lower() == "column" for c in r)), None)
        if hdr is not None:
            ci = [c.lower() for c in rows[hdr]].index("column")
            cols = [r[ci] for r in rows[hdr + 1:] if ci < len(r) and r[ci]]
            if cols != COLUMNS:
                log(logging.ERROR, "schema_column_mismatch", dictionary=cols, script=COLUMNS)
                raise SystemExit("schema_dictionary.xlsx columns differ from COLUMNS in the script - fix COLUMNS first")
            log(logging.INFO, "schema_columns_verified", n=len(cols))
    ws = sheets.get("enums")
    if ws is not None:
        for r in rows_of(ws)[1:]:
            if len(r) < 2 or r[0] not in ENUMS:
                continue
            vals = [v.strip() for v in re.sub(r"\(.*?\)", "", r[1]).split("|") if v.strip()]
            if len(vals) >= 2 and vals != ENUMS[r[0]]:
                log(logging.WARNING, "enum_taken_from_dictionary", column=r[0], dictionary=vals, script=ENUMS[r[0]])
                ENUMS[r[0]] = vals
        log(logging.INFO, "schema_enums_verified", enums={k: v for k, v in ENUMS.items()})

