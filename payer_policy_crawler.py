#!/usr/bin/env python3
"""Module 5/5 - orchestration + entry point: PayerCrawler, export and main().  Run:  python payer_policy_crawler.py

Part of the Payer Policy Document Discovery Crawler (v2). Logic is unchanged from the single-file version."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import csv
import gzip
import json
import logging
import os
import re
import time
import traceback
from collections import defaultdict
from pydantic import ValidationError
from typing import Any, Callable, Optional
from urllib.parse import urlparse

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    etree = None

from crawler_config import (RELATED_DOMAIN_NO_BRAND_EXTRA, SITEMAP_DOC_MIN_SCORE, SITEMAP_EXPLORE_AFFILIATED, SITEMAP_PAGE_CAP, SITEMAP_PAGE_MIN_SCORE, PARALLEL_PAYERS, STOP, PREFETCH_AHEAD, BROWSER_RETRY_ON_BOT_CHALLENGE, COLUMNS, DOC_CANDIDATE_MIN_SCORE, DOC_DONE_STATES, ENUMS, EXTERNAL_DOC_MIN_SCORE, EXTRA_START_URLS, HOMEPAGE_URL, HUB_INHERIT_MAX_BONUS, HUB_INHERIT_MIN_SCORE, KNOWN_PATTERN_MIN_DOCS, LLM_FOR_MISSING_FIELDS, MAX_DEPTH, MAX_DOCUMENTS_PER_PAYER, MAX_PAGES_PER_PAYER, MAX_PARSE_BYTES, MAX_RENDERS_PER_PAYER, MAX_SITEMAP_FILES, MAX_SITEMAP_URLS, MAYBE_DOC_MIN_SCORE, NAV_FOLLOW_MIN_SCORE, NOTES_MAX_CHARS, OLLAMA_MODEL, OUTPUT_DIR, PAGE_DONE_STATES, PAGE_FOLLOW_MIN_SCORE, PAYER_NAME, PAYER_REGISTRY, ProgressUI, RELATED_DOMAIN_DEPTH_BUDGET, RELATED_DOMAIN_MIN_SCORE, RESET_PAYER_STATE, REVISION_POLICY, RUN_ALL_PAYERS, SAVE_DOCUMENT_FILES, SEED_LIST_CSV, STATE_DB, STATE_JOINER, STATE_NATIONAL, UNFETCHED_DOC_ROW_MIN_SCORE, log, setup_logging, utc_now_iso)
from crawler_io import (Browser, FetchResult, Fetcher, Inspection, Link, RenderResult, VIA_BY_ORIGIN, extract_links, inspect_content, inspect_html, is_auth_block, links_from_json, needs_js, sniff_file_type)
from crawler_state import (LLMClassifier, PolicyRow, StateDB, load_schema_dictionary)
from crawler_text import (header_negative, doc_type_hint, type_keyword_hit, DOC_EXTS, THIRD_PARTY_BLOCKLIST, classify_doc_type, clean_anchor, clean_text, content_fingerprint, detect_lob, detect_state, extract_dates, extract_policy_number, html_policy_evidence, is_cdn_host, is_good_title, join_notes, link_kind, normalize_url, parse_date, reg_domain, register_brands, score_link, sha256_hex, short_url, text_fingerprint, title_from_filename, trim_title, url_ext, url_key, url_words)


# =============================================================================
# 15. PAYER CRAWLER (orchestration)
# =============================================================================
COMMON_SUBDOMAINS = ["www", "provider", "providers", "professionals", "pharmacy", "medicalpolicy", "medpolicy", "policies"]
COMMON_HUB_PATHS = [
    "/providers", "/provider", "/for-providers", "/providers/resources", "/provider/resources", "/medical-policies",
    "/medicalpolicies", "/medical-policy", "/medicalpolicy", "/policies", "/provider/policies", "/providers/policies",
    "/clinical-policies", "/coverage-policies", "/coverage-policy", "/policies-and-guidelines", "/policies-guidelines",
    "/payment-policies", "/reimbursement-policies", "/prior-authorization", "/priorauthorization", "/prior-auth",
    "/precertification", "/pharmacy", "/providers/pharmacy", "/formulary", "/formularies", "/drug-list", "/druglist",
    "/drug-lists", "/pharmacy/formulary", "/providers/formulary", "/clinical-guidelines", "/utilization-management",
    "/forms", "/providers/forms", "/provider-forms", "/provider-manual", "/providers/provider-manual",
]
_API_URL_RX = re.compile(r"/api/|/services?/|/rest/|graphql|getdocument|getfile|/download\?|\.ashx", re.I)


def _hints(item: dict) -> dict:
    h = item.get("hints")
    if isinstance(h, str):
        try:
            return json.loads(h or "{}")
        except json.JSONDecodeError:
            return {}
    return dict(h or {})


def _charset(content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    return m.group(1) if m else ""


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def in_scope_candidate(domain: str) -> bool:
    return domain not in THIRD_PARTY_BLOCKLIST and not domain.endswith((".gov", ".edu"))


# registered domain -> owning payer, from the registry: a domain that belongs to ANOTHER payer is never a "related domain"
_DOMAIN_OWNER: dict[str, str] = {}
for _p in PAYER_REGISTRY:
    for _d in list(_p.get("affiliated_domains", [])) + [_p.get("hint_host", "")]:
        if _d:
            _DOMAIN_OWNER.setdefault(reg_domain(_d.split("/")[0].split(":")[0]), _p["payer_name"])


class PayerCrawler:
    def __init__(self, payer: dict, db: StateDB, fetcher: Fetcher, browser: Browser, llm: LLMClassifier, ui: ProgressUI) -> None:
        self.p = payer
        self.name = payer["payer_name"]
        self.alias = payer.get("payer_alias", "")
        self.footprint = payer.get("footprint_state", "") or ""
        self.db, self.fx, self.br, self.llm, self.ui = db, fetcher, browser, llm, ui
        self.affiliated = {reg_domain(d) for d in payer.get("affiliated_domains", []) if d}
        register_brands([self.name, self.alias] + list(payer.get("brand_terms", [])))
        brands = [b for b in [self.name, self.alias] + list(payer.get("brand_terms", [])) if b and len(b) >= 3]
        self.brand_rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(b) for b in sorted(set(brands), key=len, reverse=True)) + r")(?!\w)",
                                   re.I) if brands else None
        self.brand_tokens = {re.sub(r"[^a-z0-9]", "", b.lower()) for b in brands if len(re.sub(r"[^a-z0-9]", "", b)) >= 4}
        self._load_state()

    def _related_threshold(self, dom: str, link: Link, page_ctx: str) -> float:
        """Score an off-domain page needs to open a related domain. A domain that carries the payer's name (or a link whose
        text/context names the payer) needs the base score; an unrelated-looking domain needs RELATED_DOMAIN_NO_BRAND_EXTRA more."""
        base = RELATED_DOMAIN_MIN_SCORE
        label = re.sub(r"[^a-z0-9]", "", dom.lower())
        named = any(tok in label for tok in self.brand_tokens)
        if not named and self.brand_rx is not None:
            named = bool(self.brand_rx.search(" ".join([link.anchor, link.heading, link.parent, page_ctx])))
        return base if named else base + RELATED_DOMAIN_NO_BRAND_EXTRA

    def _load_state(self) -> None:
        self.home_domains: set[str] = set(self.db.get(self.name, "home_domains", [])) | self.affiliated
        self.related_domains: set[str] = set(self.db.get(self.name, "related_domains", []))
        self.home_hosts: list[str] = self.db.get(self.name, "home_hosts", [])
        self.renders_at_start = self.br.renders
        c = self.db.status_counts(self.name)
        self.pages_done = sum(c.get(("page", s), 0) for s in PAGE_DONE_STATES)
        self.docs_done = sum(c.get((k, s), 0) for k in ("doc", "maybe_doc") for s in DOC_DONE_STATES)

    def _phase(self, name: str) -> None:
        self.ui.phase(name)
        self.ui.update(self.db, self.name, force=True)
        log(logging.INFO, "phase", payer=self.name, phase=name)

    # ---------------------------------------------------------------- phases
    def run(self) -> None:
        start = time.time()
        log(logging.INFO, "payer_start", payer=self.name, homepage=self.p["homepage"])
        if RESET_PAYER_STATE and self.p.get("is_current"):
            self.db.reset_payer(self.name)
            self._load_state()
        n = self.db.recover_in_progress(self.name)
        if n:
            log(logging.INFO, "resume", payer=self.name, requeued=n)
        if not self.db.phase_done(self.name, "resolve"):
            self._phase("resolve host")
            self.phase_resolve()
        if not self.db.phase_done(self.name, "sitemaps"):
            self._phase("sitemaps")
            self.phase_sitemaps()
        self._phase("crawl")
        self.phase_crawl()
        if not self.db.phase_done(self.name, "known_patterns"):
            if self.db.accepted_count(self.name) < KNOWN_PATTERN_MIN_DOCS:
                self._phase("known URL patterns")
                self.phase_known_patterns()
                self._phase("crawl (pattern pages)")
                self.phase_crawl()
            self.db.mark_phase(self.name, "known_patterns")
        self.ui.update(self.db, self.name, force=True)
        log(logging.INFO, "payer_done", payer=self.name, accepted=self.db.accepted_count(self.name),
            minutes=round((time.time() - start) / 60, 1))

    def _save_domains(self) -> None:
        self.db.put(self.name, "home_domains", sorted(self.home_domains))
        self.db.put(self.name, "related_domains", sorted(self.related_domains))
        self.db.put(self.name, "home_hosts", self.home_hosts)

    def phase_resolve(self) -> None:
        hp = normalize_url(self.p["homepage"] if "://" in self.p["homepage"] else f"https://{self.p['homepage']}")
        host = urlparse(hp).hostname or ""
        base_domain = reg_domain(host)
        hint = self.p.get("hint_host") or host
        self.home_domains.add(base_domain)
        seeds: list[tuple[str, str]] = [(hp, f"seed:{hint}")]
        r = self.fx.fetch(hp)                      # robots.txt honoured
        if r.error == "robots_disallowed":
            log(logging.WARNING, "homepage_robots_disallowed", payer=self.name, url=hp)
        if r.final_url and url_key(r.final_url) != url_key(hp):
            self.home_domains.add(reg_domain(urlparse(r.final_url).hostname or ""))
            log(logging.INFO, "homepage_redirect", payer=self.name, frm=hp, to=r.final_url)
            seeds.append((r.final_url, f"seed:{hint} > redirect({r.final_url})"))
        if r.blocked or (r.error and r.error != "robots_disallowed"):
            log(logging.WARNING, "homepage_problem", payer=self.name, blocked=r.blocked, error=r.error, status=r.status)
        is_ip = bool(re.fullmatch(r"[\d.]+|\[[0-9a-f:]+\]|localhost", host))
        probes: list[tuple[str, str]] = []
        for sub in ([] if is_ip else COMMON_SUBDOMAINS):    # documents often live on provider./www. hosts
            cand = f"https://{sub}.{base_domain}/"
            if urlparse(cand).hostname in (host, urlparse(r.final_url or "").hostname):
                continue
            probes.append((sub, cand))
        # probes go to different hosts, so they run concurrently (robots.txt still honoured for each); results are
        # processed in the original order
        for (sub, cand), pr in zip(probes, self.fx.fetch_many([c for _, c in probes])):
            if pr.status and pr.status < 400 and not pr.error and not pr.blocked:
                if reg_domain(urlparse(pr.final_url).hostname or "") in self.home_domains:
                    seeds.append((pr.final_url, f"seed:{hint} > subdomain-probe({sub}.{base_domain})"))
                    log(logging.INFO, "subdomain_found", payer=self.name, url=pr.final_url)
        for u in (EXTRA_START_URLS if self.p.get("is_current") else []):
            u = normalize_url(u)
            if u:
                seeds.append((u, f"seed:{hint} > extra_start_url"))
                self.home_domains.add(reg_domain(urlparse(u).hostname or ""))
        self.home_hosts = list(dict.fromkeys(f"{urlparse(u).scheme}://{urlparse(u).netloc}" for u, _ in seeds))
        for u, path in seeds:
            self.db.enqueue(self.name, dict(url=u, kind="page", priority=100, depth=0, budget=MAX_DEPTH, source_page_url=u,
                                            discovery_path=f"{path} > page({u})", link_score=10, route="seed",
                                            hints={"_via": "css_selector"}))
        self.db.commit()
        self._save_domains()
        self.db.mark_phase(self.name, "resolve")

    def phase_sitemaps(self) -> None:
        hint = self.p.get("hint_host") or ""
        sitemap_urls: list[tuple[str, str]] = []
        origins = list(self.home_hosts)
        if SITEMAP_EXPLORE_AFFILIATED:
            origins += self._affiliated_origins(origins)
        for origin in origins:
            robots_url = f"{origin}/robots.txt"
            for sm in self.fx.sitemaps_from_robots(origin + "/"):
                sitemap_urls.append((normalize_url(sm, origin), f"seed:{hint} > robots.txt({robots_url})"))
            for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
                sitemap_urls.append((origin + path, f"seed:{hint} > sitemap_guess"))
        seen: set[str] = set()
        queue = list(dict.fromkeys(sitemap_urls))
        n_urls = n_files = n_docs_q = n_pages_q = n_low_pages = 0
        while queue and n_files < MAX_SITEMAP_FILES and n_urls < MAX_SITEMAP_URLS:
            if STOP.is_set():
                raise KeyboardInterrupt
            sm_url, path = queue.pop(0)
            if not sm_url or sm_url in seen:
                continue
            seen.add(sm_url)
            self.fx.prefetch([u for u, _ in queue[:3] if u and u not in seen], keep=sm_url)
            r = self.fx.fetch_prefetched(sm_url)
            n_files += 1
            self.ui.update(self.db, self.name, sm_url)
            if r.error or r.status != 200 or not r.content:
                continue
            data = r.content
            if data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    continue
            parsed = self._parse_sitemap(data)
            if parsed is None:
                continue
            kind, entries = parsed
            this_path = f"{path} > sitemap({sm_url})"
            if kind == "index":
                children = sorted(entries, key=lambda e: -score_link(e[0], is_page=True)[0])
                queue = [(c[0], this_path) for c in children] + queue
                continue
            for loc, lastmod in entries:
                n_urls += 1
                u = normalize_url(loc)
                if not u:
                    continue
                dom = reg_domain(urlparse(u).hostname or "")
                kindu = link_kind(u)
                if kindu in ("junk", "sitemap"):
                    continue
                sc, sig = score_link(u, is_page=(kindu == "page"))
                hints: dict[str, str] = {"_via": "sitemap"}
                if lastmod:
                    hints["sitemap_lastmod"] = lastmod
                doc_min = SITEMAP_DOC_MIN_SCORE if kindu == "doc" else DOC_CANDIDATE_MIN_SCORE
                if kindu in ("doc", "maybe_doc") and sc >= doc_min and dom in self.home_domains:
                    self._enqueue_doc(u, kindu, sc, sig, "", f"{this_path} > url({u})", 1, "", "", "", hints, "sitemap")
                    n_docs_q += 1
                elif kindu == "page" and dom in self.home_domains and sc >= SITEMAP_PAGE_MIN_SCORE:
                    strong = sc >= PAGE_FOLLOW_MIN_SCORE + 1
                    if not strong and n_low_pages >= SITEMAP_PAGE_CAP:
                        continue         # low-evidence pages (possible native-HTML policies with opaque slugs) are capped
                    n_low_pages += 0 if strong else 1
                    n_pages_q += 1
                    self.db.enqueue(self.name, dict(url=u, kind="page", priority=sc, depth=1, budget=MAX_DEPTH - 1,
                                                    source_page_url="", discovery_path=f"{this_path} > page({u})",
                                                    link_score=sc, signals=sig, route="sitemap", hints=hints))
            self.db.commit()
        if queue and (n_files >= MAX_SITEMAP_FILES or n_urls >= MAX_SITEMAP_URLS):
            log(logging.WARNING, "sitemap_limit_reached", payer=self.name, files=n_files, urls=n_urls, unread_sitemaps=len(queue),
                hint="raise MAX_SITEMAP_URLS / MAX_SITEMAP_FILES; the link crawl still runs")
        log(logging.INFO, "sitemaps_done", payer=self.name, sitemap_files=n_files, urls_seen=n_urls,
            docs_queued=n_docs_q, pages_queued=n_pages_q)
        self.db.mark_phase(self.name, "sitemaps")

    def _affiliated_origins(self, existing: list[str]) -> list[str]:
        """Affiliated domains (registry) that have a real robots.txt on www.<domain>: their sitemaps are read too."""
        have = {urlparse(o).netloc.lower() for o in existing}
        cands = [f"https://www.{d}" for d in sorted(self.affiliated) if d and f"www.{d}" not in have and d not in have]
        if not cands:
            return []
        out = []
        for o, r in zip(cands, self.fx.fetch_many([f"{c}/robots.txt" for c in cands])):
            if r.status == 200 and not r.error and not r.blocked and b"user-agent" in (r.content or b"")[:20000].lower():
                out.append(o)
        log(logging.INFO, "affiliated_sitemap_origins", payer=self.name, tried=len(cands), usable=len(out))
        return out

    @staticmethod
    def _parse_sitemap(data: bytes) -> Optional[tuple[str, list[tuple[str, str]]]]:
        if etree is None:
            locs = re.findall(rb"<loc>\s*(.*?)\s*</loc>", data, re.S)
            kind = "index" if b"<sitemapindex" in data[:2000] else "urlset"
            return kind, [(l.decode("utf-8", "ignore"), "") for l in locs]
        try:
            root = etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False))
        except Exception:
            return None
        if root is None:
            return None
        tag = etree.QName(root).localname.lower()
        entries = []
        for el in root:
            if not isinstance(el.tag, str):
                continue
            loc = lastmod = ""
            for ch in el:
                if not isinstance(ch.tag, str):
                    continue
                name = etree.QName(ch).localname.lower()
                if name == "loc":
                    loc = (ch.text or "").strip()
                elif name == "lastmod":
                    lastmod = (ch.text or "").strip()
            if loc:
                entries.append((loc, lastmod))
        return ("index" if tag == "sitemapindex" else "urlset"), entries

    def phase_known_patterns(self) -> None:
        hint = self.p.get("hint_host") or ""
        log(logging.INFO, "known_patterns_start", payer=self.name, accepted_so_far=self.db.accepted_count(self.name))
        todo = [(origin, path, f"{origin}{path}") for origin in self.home_hosts for path in COMMON_HUB_PATHS]
        heads = self.fx.fetch_many([u for _, _, u in todo], method="HEAD")
        for (origin, path, u), r in zip(todo, heads):
            self.ui.update(self.db, self.name, u)
            if r.status in (405, 501) or (r.error and r.error != "robots_disallowed"):
                r = self.fx.fetch(u)
            if r.error or not r.status or r.status >= 400 or r.blocked:
                continue
            final = r.final_url or u
            if reg_domain(urlparse(final).hostname or "") not in self.home_domains | self.related_domains:
                continue
            sc, sig = score_link(final, is_page=True)
            self.db.enqueue(self.name, dict(url=final, kind="page", priority=max(sc, 3), depth=1, budget=MAX_DEPTH - 1,
                                            source_page_url="",
                                            discovery_path=f"seed:{hint} > url_pattern({path} on {origin}) > page({final})",
                                            link_score=max(sc, 3), signals=sig, route="known_pattern",
                                            hints={"_via": "url_pattern"}))
        self.db.commit()

    # ---------------------------------------------------------------- crawl loop
    def phase_crawl(self) -> None:
        while True:
            if STOP.is_set():
                raise KeyboardInterrupt      # parallel run interrupted: same clean stop as Ctrl+C in a single run
            if self.docs_done >= MAX_DOCUMENTS_PER_PAYER:
                log(logging.WARNING, "doc_budget_exhausted", payer=self.name)
                self._skip_pending(("doc", "maybe_doc"), "skipped_budget")
                break
            item = self.db.next_item(self.name, ("doc", "maybe_doc"))
            if item is None:
                if self.pages_done >= MAX_PAGES_PER_PAYER:
                    log(logging.WARNING, "page_budget_exhausted", payer=self.name)
                    self._skip_pending(("page",), "skipped_budget")
                    break
                item = self.db.next_item(self.name, ("page",))
                if item is None:
                    break
            self.ui.update(self.db, self.name, item["url"])
            if PREFETCH_AHEAD > 0 and item["kind"] != "page":   # next documents are fetched while this one is processed
                self.fx.prefetch(self.db.peek_doc_urls(self.name, PREFETCH_AHEAD), keep=item["url"])
            try:
                if item["kind"] == "page":
                    self.process_page(item)
                    self.pages_done += 1
                else:
                    self.process_document(item)
                    self.docs_done += 1
            except KeyboardInterrupt:
                self.db.finish(self.name, item["key"], "pending", "interrupted")
                raise
            except Exception as e:
                log(logging.ERROR, "item_error", payer=self.name, url=item["url"], error=repr(e)[:300],
                    trace=traceback.format_exc()[-1200:])
                self.db.finish(self.name, item["key"], "failed", f"exception:{type(e).__name__}")
        self.ui.update(self.db, self.name, force=True)

    def _skip_pending(self, kinds: tuple[str, ...], status: str) -> None:
        self.db.con.execute(f"UPDATE frontier SET status=?, reason='budget' WHERE payer=? AND status='pending' "
                            f"AND kind IN ({','.join('?' * len(kinds))})", (status, self.name, *kinds))
        self.db.commit()

    # ---------------------------------------------------------------- pages
    def process_page(self, item: dict) -> None:
        url, key = item["url"], item["key"]
        r = self.fx.fetch_prefetched(url)
        if r.error == "robots_disallowed":
            log(logging.INFO, "robots_disallowed", payer=self.name, url=url)
            self.db.finish(self.name, key, "skipped_robots", "robots.txt disallow")
            return
        if r.error:
            self.db.finish(self.name, key, "failed", r.error)
            return
        if r.blocked:
            if BROWSER_RETRY_ON_BOT_CHALLENGE and r.blocked.startswith("bot_challenge") and self._can_render():
                rr = self.br.render(url)
                if not rr.blocked and not rr.error and rr.snapshots:
                    log(logging.INFO, "challenge_passed_by_plain_render", payer=self.name, url=url)
                    self._handle_rendered(item, rr, r)
                    return
            log(logging.WARNING, "page_blocked", payer=self.name, url=url, reason=r.blocked, status=r.status)
            self.db.block_page(self.name, url, r.blocked, r.status)
            self.db.finish(self.name, key, "blocked", r.blocked)
            return
        if r.status >= 400:
            self.db.finish(self.name, key, "failed", f"http_{r.status}")
            return
        ft = sniff_file_type(r.content, r.content_type, r.final_url or url)
        if ft != "html":   # an extension-less link turned out to be a document
            self.db.con.execute("UPDATE frontier SET kind='maybe_doc' WHERE payer=? AND key=?", (self.name, key))
            self.docs_done += 1
            self.process_document(item, prefetched=r)
            return
        html = r.content.decode(_charset(r.content_type) or "utf-8", "ignore")
        links, ctx = extract_links(html, r.final_url or url)
        page_url = r.final_url or url
        self._maybe_html_document(item, r, html, ctx)
        n_doc_links = sum(1 for l in links if link_kind(l.url) in ("doc", "maybe_doc"))
        if self._can_render() and (needs_js(html, len(links)) or (float(item["link_score"] or 0) >= 5 and n_doc_links < 3)):
            rr = self.br.render(page_url)
            if rr.snapshots and not rr.blocked:
                self._handle_rendered(item, rr, r)
                return
        self._enqueue_links(item, links, ctx, page_url, extra_step="")
        self.db.finish(self.name, key, "done", f"links={len(links)}")

    def _can_render(self) -> bool:
        return self.br.available and (self.br.renders - self.renders_at_start) < MAX_RENDERS_PER_PAYER

    def _handle_rendered(self, item: dict, rr: RenderResult, static: FetchResult) -> None:
        page_url = rr.final_url or item["url"]
        all_links: dict[str, Link] = {}
        ctx = ""
        for label, snap in rr.snapshots:     # the first snapshot that shows a link decides its interaction label
            ls, c = extract_links(snap, page_url)
            ctx = ctx or c
            for l in ls:
                k = url_key(l.url)
                if k not in all_links:
                    l.label = label
                    all_links[k] = l
                elif not all_links[k].anchor and l.anchor:
                    l.label = all_links[k].label
                    all_links[k] = l
        for label, nav, text in rr.nav_links:
            u = normalize_url(nav)
            if u and url_key(u) not in all_links:
                all_links[url_key(u)] = Link(url=u, anchor=clean_text(text, 120), origin="option", label=label)
        self._enqueue_links(item, list(all_links.values()), ctx, page_url, extra_step=" > render:headless")
        xhr_links = 0
        for api_url, payload in rr.json_payloads:
            found: list[Link] = []
            links_from_json(payload, page_url, found)
            if found:
                xhr_links += len(found)
                self._enqueue_links(item, found, ctx, page_url, extra_step=f" > render:headless > xhr({api_url[:200]})")
        if rr.snapshots:
            if static.content and not static.blocked and static.status == 200:
                raw = static                                   # hash/size describe the bytes the server sent to plain HTTP
            else:
                body = rr.main_body or rr.snapshots[0][1].encode("utf-8")
                raw = FetchResult(url=item["url"], final_url=page_url, status=rr.status or 200, first_status=rr.status or 200,
                                  content=body, content_type="text/html", sha256=sha256_hex(body), size=len(body),
                                  via_browser=True, fetched_at=rr.fetched_at or utc_now_iso())
            for snap_html in dict.fromkeys([rr.snapshots[-1][1], rr.snapshots[0][1]]):
                if self._maybe_html_document(item, raw, snap_html, ctx):
                    break
        self.db.finish(self.name, item["key"], "done", f"rendered links={len(all_links)} xhr_links={xhr_links}")

    def _enqueue_links(self, item: dict, links: list[Link], page_ctx: str, page_url: str, extra_step: str) -> None:
        depth = int(item["depth"]) + 1
        budget = (int(item["budget"]) if item["budget"] is not None else MAX_DEPTH) - 1
        base_host = urlparse(page_url).hostname or ""
        parent_path = item["discovery_path"] + extra_step
        parent_route = item.get("route") or "crawl"
        hub_score = float(item["link_score"] or 0)
        if page_ctx:
            hub_score = max(hub_score, score_link("", anchor=page_ctx, is_page=True)[0])
        page_is_hub = int(item["depth"]) > 0 and hub_score >= HUB_INHERIT_MIN_SCORE
        for l in links:
            kind = link_kind(l.url)
            if kind == "junk":
                continue
            host = urlparse(l.url).hostname or ""
            dom = reg_domain(host)
            if dom in THIRD_PARTY_BLOCKLIST:
                continue
            sc, sig = score_link(l.url, l.anchor, l.heading, l.parent, page_ctx, is_page=(kind == "page"))
            via = VIA_BY_ORIGIN.get(l.origin, "css_selector")
            if l.origin == "xhr":
                route = "xhr_json"
            elif "render:headless" in extra_step:
                route = "js_render"
            elif parent_route in ("sitemap", "known_pattern", "related_domain", "xhr_json", "js_render"):
                route = parent_route
            else:
                route = "crawl"
            step_name = {"a": "link", "option": "option", "iframe": "embed", "onclick": "onclick", "script": "script_url",
                         "xhr": "item"}.get(l.origin, "link")
            label = clean_anchor(l.anchor)[:80].replace("'", "’")
            interaction = f"{l.label} > " if l.label else ""
            step = (f"{interaction}{step_name}:'{label}' ({short_url(l.url, base_host)})" if label
                    else f"{interaction}{step_name}:({short_url(l.url, base_host)})")
            path = f"{parent_path} > {step}"
            hints = dict(l.hints or {})
            hints["_via"] = via
            in_scope = dom in self.home_domains or dom in self.related_domains
            if kind in ("doc", "maybe_doc"):
                if page_is_hub and sc >= 0:      # documents listed on a policy hub inherit part of its evidence
                    sc = round(sc + min(HUB_INHERIT_MAX_BONUS, 0.25 * hub_score), 2)
                    sig = sig + ["hub_inherit"]
                # CDN documents often have generic anchors ("Download") and opaque URLs: a document file on a known CDN that
                # is linked from an in-scope page is fetched and inspected - unless the link text itself is clearly
                # unwanted (privacy, careers ... score below -3), so those are not downloaded at all.
                if is_cdn_host(host) and url_ext(l.url) in DOC_EXTS and sc > -3:
                    sc = max(sc, DOC_CANDIDATE_MIN_SCORE)
                    sig = sig + ["cdn_doc_baseline"]
                threshold = DOC_CANDIDATE_MIN_SCORE if kind == "doc" else MAYBE_DOC_MIN_SCORE
                external = not in_scope and not is_cdn_host(host)
                if external:
                    threshold = max(threshold, EXTERNAL_DOC_MIN_SCORE)
                    hints["_external"] = True
                if sc >= threshold:
                    self._enqueue_doc(l.url, kind, sc, sig, page_url, path, depth, l.anchor, l.heading, page_ctx, hints, route)
                elif sc > -3:
                    threshold_val = DOC_CANDIDATE_MIN_SCORE if kind == "doc" else MAYBE_DOC_MIN_SCORE
                    if external:
                        threshold_val = max(threshold_val, EXTERNAL_DOC_MIN_SCORE)

                    reason_text = (f"Link score ({sc:.2f}) is below the minimum threshold ({threshold_val:.2f}) "
                                   f"required for {'an external host' if external else 'this document type'}. "
                                   f"Anchor text: '{clean_anchor(l.anchor)[:100]}'.")

                    self.db.skip_link(self.name, l.url, page_url, l.anchor, sc, hub_score, reason_text, l.heading, page_ctx)
                continue
            if kind == "sitemap" or budget <= 0 or depth > MAX_DEPTH:
                continue
            if in_scope:
                follow = sc >= PAGE_FOLLOW_MIN_SCORE or (int(item["depth"]) == 0 and sc >= NAV_FOLLOW_MIN_SCORE)
                if follow:
                    self.db.enqueue(self.name, dict(url=l.url, kind="page", priority=sc - 0.5 * depth, depth=depth, budget=budget,
                                                    source_page_url=page_url, discovery_path=path, anchor=l.anchor, heading=l.heading,
                                                    page_ctx=page_ctx, link_score=sc, signals=sig,
                                                    route=route if route != "xhr_json" else "js_render", hints=hints))
            elif (in_scope_candidate(dom) and _DOMAIN_OWNER.get(dom, self.name) == self.name
                  and sc >= self._related_threshold(dom, l, page_ctx)):
                # off-domain hub strongly about policies (state plan site, document portal) -> related domain.
                # Documents found there must still name the payer (brand check in _finalize).
                self.related_domains.add(dom)
                self._save_domains()
                log(logging.INFO, "related_domain_added", payer=self.name, domain=dom, via=l.url, score=sc)
                self.db.enqueue(self.name, dict(url=l.url, kind="page", priority=sc - 0.5 * depth, depth=depth,
                                                budget=min(budget, RELATED_DOMAIN_DEPTH_BUDGET), source_page_url=page_url,
                                                discovery_path=path, anchor=l.anchor, heading=l.heading, page_ctx=page_ctx,
                                                link_score=sc, signals=sig, route="related_domain", hints=hints))
        self.db.commit()

    def _enqueue_doc(self, url: str, kind: str, sc: float, sig: list, source: str, path: str, depth: int,
                     anchor: str, heading: str, page_ctx: str, hints: dict, route: str) -> None:
        self.db.enqueue(self.name, dict(url=url, kind=kind, priority=sc + 3, depth=depth, budget=0, source_page_url=source,
                                        discovery_path=path, anchor=anchor, heading=heading, page_ctx=page_ctx, hints=hints,
                                        link_score=sc, signals=sig, route=route))

    # ---------------------------------------------------------------- html page as a document
    def _maybe_html_document(self, item: dict, r: FetchResult, html: str, page_ctx: str) -> bool:
        """Native HTML policy / manual / bulletin pages are documents too. True when the page passed the evidence test."""
        if int(item["depth"]) == 0:
            return False
        page_url = r.final_url or item["url"]
        if self.db.doc_accepted(self.name, url_key(page_url)):
            return True
        ins = inspect_html(html.encode("utf-8", "ignore"), page_url, self.alias)
        is_doc, cscore, csig = html_policy_evidence(ins, page_url)
        if not is_doc:
            return False
        doc_item = dict(item)
        doc_item["url"] = page_url
        self._finalize(doc_item, r, ins, cscore, csig, skip_finish=True)
        return True

    # ---------------------------------------------------------------- documents
    def process_document(self, item: dict, prefetched: Optional[FetchResult] = None) -> None:
        url, key = item["url"], item["key"]
        if self.db.doc_accepted(self.name, key):
            self.db.finish(self.name, key, "rejected", "duplicate_url")
            return
        r = prefetched or self.fx.fetch_prefetched(url)
        if r.error == "robots_disallowed":
            log(logging.INFO, "robots_disallowed", payer=self.name, url=url)
            self.db.finish(self.name, key, "skipped_robots", "robots.txt disallow")
            return
        if r.blocked and r.blocked.startswith("bot_challenge") and BROWSER_RETRY_ON_BOT_CHALLENGE and self.br.available:
            br = self.br.fetch_bytes(url)
            if not br.error and not br.blocked and 0 < br.status < 400:
                r = br
        if r.error or r.blocked or r.status >= 400 or r.status == 0:
            self._finalize_unfetched(item, r)
            return
        final_key = url_key(r.final_url) if r.final_url else key
        if final_key != key and self.db.doc_accepted(self.name, final_key):
            self.db.finish(self.name, key, "rejected", f"redirects_to_known_document:{r.final_url}")
            return
        ins = inspect_content(r.content, r.content_type, r.final_url or url, self.alias)
        cscore, csig = content_fingerprint(ins.text)
        if ins.file_type == "html":
            # a "document" link that serves HTML: viewer page, landing page or an HTML policy page
            html = r.content.decode(_charset(r.content_type) or "utf-8", "ignore")
            links, ctx = extract_links(html, r.final_url or url)
            self._enqueue_links(item, links, ctx, r.final_url or url, extra_step="")
            is_doc, cscore, csig = html_policy_evidence(ins, r.final_url or url)
            if not is_doc:
                self.db.finish(self.name, key, "rejected", "html_landing_or_index_page")
                return
        self._finalize(item, r, ins, cscore, csig)

    def _note_for_failure(self, r: FetchResult) -> tuple[str, str, bool]:
        """-> (frontier status, notes sentence, requires_auth)."""
        b = r.blocked
        if is_auth_block(b):
            where = f" (redirected to {r.final_url})" if b == "login_redirect" and r.final_url else ""
            return "blocked", f"Behind a login/registration wall ({b}){where}; not accessed, by design", True
        if b.startswith("bot_challenge"):
            vendor = b.split("_")[2] if b.count("_") >= 3 else "unknown"
            return "blocked", f"Blocked by bot protection ({vendor}, HTTP {r.status}); not bypassed", False
        if b == "forbidden_403":
            return "blocked", "HTTP 403 Forbidden without a login prompt; not bypassed", False
        if b == "rate_limited_429":
            return "blocked", "HTTP 429 rate limited after retries with backoff", False
        if b:
            return "blocked", f"Blocked ({b}, HTTP {r.status})", False
        if r.status == 0 or (r.error and not r.status):
            return "failed", f"Transport failure ({r.error or 'no response'}); http_status 0", False
        return "failed", f"HTTP {r.status} when fetching the linked document", False

    def _finalize_unfetched(self, item: dict, r: FetchResult) -> None:
        """Strong candidates that could not be retrieved still get a row (schema: non-200 rows are wanted)."""
        key, url = item["key"], item["url"]
        ls = float(item["link_score"] or 0)
        status, sentence, auth = self._note_for_failure(r)
        if ls < UNFETCHED_DOC_ROW_MIN_SCORE:
            log(logging.DEBUG, "unfetched_low_evidence_skipped", payer=self.name, url=url, reason=sentence)
            self.db.finish(self.name, key, status, r.blocked or r.error or f"http_{r.status}")
            return
        hints = _hints(item)
        anchor = clean_anchor(item.get("anchor") or "")
        title = anchor if is_good_title(anchor, self.alias) else title_from_filename(url)
        own = " ".join([anchor, url_words(url)])
        around = " ".join(x for x in [item.get("heading") or "", item.get("page_ctx") or ""] if x)
        _ht, _hk, _htier = doc_type_hint(own)
        dtype, _, reimb = classify_doc_type(own, around=around, boost=_ht if _htier == "title" else "")
        lob, _ = detect_lob(own, "", around)
        state, ssrc = detect_state(url, own, "", around, self.footprint)
        # document_url: the final URL after redirects, unless the redirect went to a login page / error host
        document_url = url if (auth or not r.final_url) else r.final_url
        notes = [sentence, "metadata from link text only; content not verified",
                 f"file type unknown (not retrieved; URL suffix .{url_ext(url)})" if url_ext(url) else "file type unknown (not retrieved)"]
        if reimb:
            notes.append("reimbursement/payment policy - typed other")
        if ssrc == "payer footprint":
            notes.append("state inferred from payer's single-state footprint")
        conf = round(min(0.55, 0.30 + 0.03 * ls), 2)
        row = self._row(item, title=title, dtype=dtype, lob=lob, state=state, file_type="other", policy_number="",
                        eff="", upd="", requires_auth="Y" if auth else "N", methods=[hints.get("_via", "css_selector")],
                        conf=conf, notes=notes, document_url=document_url, http_status=int(r.status or 0),
                        content_hash="", size=0, render_mode="headless" if r.via_browser else "static",
                        fetched_at=r.fetched_at)
        if row:
            self.db.save_doc(self.name, url_key(document_url), document_url, True, row,
                             {"unfetched": r.blocked or r.error or f"http_{r.status}", "link_score": ls}, item.get("route", ""))
            log(logging.WARNING if auth or r.blocked else logging.INFO, "doc_unfetched_recorded", payer=self.name, url=url,
                status=r.status, reason=r.blocked or r.error)
        self.db.finish(self.name, key, status, r.blocked or r.error or f"http_{r.status}")

    def _brand_named(self, ins: Inspection, anchor: str) -> bool:
        if self.brand_rx is None:
            return False
        blob = " ".join([ins.heading_title, ins.meta_title, " ".join(ins.titles), anchor, (ins.text or "")[:6000],
                         (ins.fp_text or "")[-3000:]])
        return bool(self.brand_rx.search(blob))

    def _finalize(self, item: dict, r: FetchResult, ins: Inspection, cscore: float, csig: list,
                  skip_finish: bool = False) -> None:
        key, url = item["key"], item["url"]
        document_url = r.final_url or url
        doc_key = url_key(document_url)
        ls = float(item.get("link_score") or 0)
        hints = _hints(item)
        via = hints.get("_via", "css_selector")
        anchor = clean_anchor(item.get("anchor") or "")
        around = " ".join(x for x in [item.get("heading") or "", item.get("page_ctx") or ""] if x)
        notes: list[str] = list(ins.notes)
        penalty = 0.0
        llm_used = False
        decision: dict[str, Any] = {"link_score": ls, "content_score": cscore, "content_signals": csig,
                                    "link_signals": json.loads(item.get("signals") or "[]") if isinstance(item.get("signals"), str) else []}

        def reject(reason: str) -> None:
            decision.update({"verdict": "reject", "reason": reason})
            self.db.save_doc(self.name, doc_key, document_url, False,
                             {"document_url": document_url, "anchor": anchor, "source_page_url": item.get("source_page_url", ""),
                              "document_title": clean_text(ins.heading_title or (ins.titles[0] if ins.titles else "") or ins.meta_title
                                                           or anchor, 300),
                              "file_type": ins.file_type},
                             decision, item.get("route", ""))
            if not skip_finish:
                self.db.finish(self.name, key, "rejected", reason)
            log(logging.DEBUG, "candidate_rejected", payer=self.name, url=document_url, ls=ls, cs=cscore, reason=reason)

        # -------- canonical duplicate (HTML)
        canon_key = ""
        if ins.canonical_url:
            cdom = reg_domain(urlparse(ins.canonical_url).hostname or "")
            if cdom in self.home_domains | self.related_domains:
                canon_key = url_key(ins.canonical_url)
                if canon_key != doc_key and self.db.doc_accepted(self.name, canon_key):
                    return reject(f"duplicate: canonical URL {ins.canonical_url} already recorded")
        decision["canonical_key"] = canon_key if canon_key and canon_key != doc_key else ""

        # -------- ownership: documents on hosts the payer does not own must name the payer
        host = urlparse(document_url).hostname or ""
        dom = reg_domain(host)
        if dom not in self.home_domains and not is_cdn_host(host):
            if not self._brand_named(ins, anchor):
                return reject(f"hosted on {dom} and does not name {self.name}")
            notes.append(f"hosted on {dom} (payer named in the document)")
            penalty += 0.05

        # -------- decision (Gate 1 + Gate 2, Gate 3 when ambiguous)
        own0 = " ".join(x for x in [ins.heading_title, " ".join(ins.titles), ins.meta_title, anchor, url_words(document_url)] if x)
        hint_type, hint_kw, hint_tier = doc_type_hint(own0, around)
        decision["type_hint"] = f"{hint_type}:{hint_kw}:{hint_tier}" if hint_type else ""
        verdict, conf, reason = self._decide(ls, cscore, ins.readable, hint_tier, min(header_negative(ins.text or ""), header_negative(own0, 400)))
        llm_res = None
        if verdict == "ambiguous":
            title_guess = ins.heading_title or (ins.titles[0] if ins.titles else "") or anchor
            llm_res = self.llm.classify(self.name, document_url, anchor, around, title_guess, ins.text)
            if llm_res is None:
                if cscore >= 3 or ls >= 5:
                    verdict, conf, reason = "accept", 0.50, "mixed rule evidence, accepted without LLM confirmation"
                    decision["llm_unavailable"] = True
                else:
                    verdict, reason = "reject", "mixed evidence, too weak without LLM"
            elif llm_res["is_policy_document"] and llm_res["confidence"] >= 0.6:
                llm_used = True
                verdict, conf = "accept", min(0.70, 0.45 + 0.25 * llm_res["confidence"])
                reason = f"LLM ({OLLAMA_MODEL}) confirmed: {llm_res['reason']}"
            else:
                verdict, reason = "reject", f"LLM rejected: {llm_res.get('reason', '')}"
        decision.update({"verdict": verdict, "reason": reason, "llm": llm_res,
                         "text_fp": text_fingerprint(ins.fp_text or ins.text)})
        if verdict != "accept":
            return reject(reason)
        if conf < 0.70:
            notes.append(reason)

        # -------- metadata (values as printed; inferences are noted and cost confidence)
        text = ins.text or ""
        header = text.lstrip()[:400]
        body = text.lstrip()[400:3000]
        title, title_src = self._pick_title(anchor, ins, document_url)
        if title_src == "filename":
            penalty += 0.05
            notes.append("title derived from the file name (no usable heading or link text)")
        own = " ".join([title, anchor, url_words(document_url)])
        pnum, psrc = extract_policy_number(text, title, ins.margin_text)
        if not pnum and hints.get("policy_number"):
            pnum, psrc = clean_text(str(hints["policy_number"]), 30), "json"
        boost = hint_type if (hint_tier == "title" or (hint_tier == "listing" and cscore < 3)) else ""
        dtype, _dsrc, reimb = classify_doc_type(own, header, body, around, pnum, boost=boost)
        lob, lsrc = detect_lob(own, header, around)
        state, ssrc = detect_state(document_url, own, header, around, self.footprint)
        if LLM_FOR_MISSING_FIELDS and ins.readable and not llm_res and (lob == "Unknown" or (dtype == "other" and not reimb)):
            llm_res = self.llm.classify(self.name, document_url, anchor, around, title, text)
            decision["llm"] = llm_res
        if llm_res:
            if dtype == "other" and not reimb and llm_res["document_type"] != "other":
                dtype, llm_used = llm_res["document_type"], True
                notes.append(f"document_type from LLM ({OLLAMA_MODEL})")
                penalty += 0.03
            if lob == "Unknown" and llm_res["line_of_business"] != "Unknown":
                lob, lsrc, llm_used = llm_res["line_of_business"], "llm", True
                notes.append(f"line_of_business from LLM ({OLLAMA_MODEL})")
                penalty += 0.03
        if reimb:
            notes.append("reimbursement/payment policy - no matching document_type, typed other")
        elif dtype == "other":
            notes.append("document type unclear - typed other")
            penalty += 0.05
        if lsrc == "listing page":
            notes.append("line_of_business taken from the listing page heading")
        if ssrc == "payer footprint":
            notes.append(f"state inferred from payer's single-state footprint ({self.footprint})")
            penalty += 0.05
        elif ssrc == "listing page":
            notes.append("state taken from the listing page heading")
        dates = extract_dates(text, ins.margin_text)
        notes += dates["notes"]
        eff, upd = dates["effective_date"], dates["last_updated_date"]
        if not eff and hints.get("effective_date"):
            d, kind = parse_date(str(hints["effective_date"])[:25])
            if d:
                eff = d
                notes.append("effective_date from the site's document index (JSON)")
        regex_used = bool((pnum and psrc in ("labeled", "format")) or eff or upd)
        if not upd and hints.get("last_updated_date"):
            d, _ = parse_date(str(hints["last_updated_date"])[:25])
            if d:
                upd = d
                notes.append("last_updated_date from the site's document index (JSON)")
        if upd and r.last_modified and r.last_modified != upd:
            notes.append(f"printed revision date {upd}; HTTP Last-Modified {r.last_modified}")
        elif not upd and r.last_modified:
            upd = r.last_modified
            notes.append("last_updated_date from HTTP Last-Modified (none printed)")
        if not upd and hints.get("sitemap_lastmod"):
            d, _k = parse_date(str(hints["sitemap_lastmod"])[:25])
            if d and d <= utc_now_iso()[:10]:
                upd = d
                notes.append("last_updated_date from the sitemap lastmod (none printed)")
        if r.truncated:
            notes.append(f"only the first {MAX_PARSE_BYTES // (1024 * 1024)} MB were parsed; hash/size cover the full file")
        if document_url != url:
            notes.append(f"redirected from {url}")
        if ins.ocr:
            penalty += 0.05
        if llm_used and not any("LLM" in n for n in notes):
            notes.append(f"LLM ({OLLAMA_MODEL}) assisted classification")

        methods = [via]
        if ins.readable:
            methods.append({"pdf": "pdf_text", "html": "css_selector"}.get(ins.file_type, "regex"))
        if regex_used:
            methods.append("regex")
        if r.via_browser:
            render_mode = "headless"
        elif via == "json_api" and _API_URL_RX.search(document_url):
            render_mode = "api"
        else:
            render_mode = "static"
        conf = max(0.05, min(0.99, conf - penalty))
        if conf < 0.70 and not notes:
            notes.append(f"confidence {conf:.2f}: {reason}")
        file_type = ins.file_type if ins.file_type in ENUMS["file_type"] else "other"
        row = self._row(item, title=title, dtype=dtype, lob=lob, state=state, file_type=file_type, policy_number=pnum,
                        eff=eff, upd=upd, requires_auth="N", methods=methods, conf=conf, notes=notes,
                        document_url=document_url, http_status=r.status, content_hash=r.sha256, size=r.size,
                        render_mode=render_mode, fetched_at=r.fetched_at)
        if row is None:
            if not skip_finish:
                self.db.finish(self.name, key, "failed", "row_validation_failed")
            return
        self.db.save_doc(self.name, doc_key, document_url, True, row, decision, item.get("route", ""))
        if SAVE_DOCUMENT_FILES and r.content:
            d = os.path.join(OUTPUT_DIR, "files", _safe(self.name))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{r.sha256[:16]}.{file_type}"), "wb") as fh:
                fh.write(r.content)
        if not skip_finish:
            self.db.finish(self.name, key, "accepted", f"{dtype} conf={conf:.2f}")
        log(logging.DEBUG, "document_accepted", payer=self.name, type=dtype, conf=round(conf, 2), title=title[:80], url=document_url)

    @staticmethod
    def _decide(ls: float, cs: float, readable: bool, hint_tier: str = "", hdr_neg: float = 0.0) -> tuple[str, float, str]:
        # A provider manual / bulletin / PA document whose title or link text carries a type keyword is judged on how it OPENS:
        # its table of contents may cross-reference "Member Handbook", "Evidence of Coverage" or "Notice of Privacy Practices"
        # (each scores negative), which must not sink it. It is rejected only if its opening text itself looks like one of them.
        if (hdr_neg <= -4) if hint_tier else (cs <= -4):
            return "reject", 0.0, "negative content fingerprint (privacy notice, EOC, press release ...)"
        if cs >= 6:
            return "accept", 0.80 + min(0.15, (cs - 6) * 0.015 + (0.05 if ls >= 4 else 0)), "strong content fingerprint"
        if cs >= 3 and ls >= 4:
            return "accept", 0.72 + min(0.13, (cs - 3) * 0.02 + (ls - 4) * 0.01), "link context and content agree"
        if ls >= 7 and cs >= 1:
            return "accept", 0.68, "strong link context, weak content confirmation"
        if not readable and ls >= 6:
            return "accept", 0.52, "strong link context; content not machine-readable, not verified"
        if hint_tier == "title" or (hint_tier == "listing" and (cs >= 1 or not readable)):
            return ("accept", round(0.62 + (0.04 if cs >= 2 else 0.0) + (0.02 if ls >= 4 else 0.0), 2),
                    "document-type keywords in the title / link text" if hint_tier == "title"
                    else "document-type keywords in the listing page heading; content consistent")
        if cs >= 2 or ls >= 3:
            return "ambiguous", 0.0, "mixed evidence"
        return "reject", 0.0, "no policy evidence"

    def _pick_title(self, anchor: str, ins: Inspection, url: str) -> tuple[str, str]:
        """Schema: prefer the document's own <title>/heading over the anchor text; fall back to anchor text."""
        if ins.file_type == "html":
            order = [("document", t) for t in ins.titles] + [("anchor", anchor)]
        else:
            order = [("document", ins.heading_title), ("anchor", anchor), ("document", ins.meta_title)]
        for src, t in order:
            t = trim_title(t or "")
            if t and is_good_title(t, self.alias):
                return clean_text(t, 300), src
        return title_from_filename(url), "filename"

    def _row(self, item: dict, *, title: str, dtype: str, lob: str, state: str, file_type: str, policy_number: str,
             eff: str, upd: str, requires_auth: str, methods: list[str], conf: float, notes: list[str], document_url: str,
             http_status: int, content_hash: str, size: int, render_mode: str, fetched_at: str) -> Optional[dict]:
        hashed = bool(content_hash) and http_status == 200 and requires_auth == "N"
        via = methods[0] if methods else "css_selector"
        source = item.get("source_page_url") or ""
        if via in ("sitemap", "url_pattern"):
            source = ""                      # schema: empty when the URL came from a sitemap / constructed pattern
        elif not source:
            source = document_url
        data = {
            "payer_name": self.name, "payer_alias": self.alias, "state_or_region": state, "line_of_business": lob,
            "document_title": clean_text(title, 300), "document_type": dtype, "document_url": document_url,
            "source_page_url": source, "discovery_path": clean_text(item.get("discovery_path") or "", 3000),
            "file_type": file_type, "policy_number": policy_number, "effective_date": eff, "last_updated_date": upd,
            "http_status": int(http_status or 0), "content_hash_sha256": content_hash if hashed else "",
            "file_size_bytes": int(size) if hashed else 0, "requires_auth": requires_auth, "render_mode": render_mode,
            "extraction_method": "|".join(dict.fromkeys(m for m in methods if m)), "confidence_score": round(conf, 2),
            "scrape_timestamp_utc": fetched_at or utc_now_iso(), "notes": join_notes(notes),
        }
        try:
            return PolicyRow(**data).as_output()
        except ValidationError as e:
            log(logging.ERROR, "row_validation_failed", payer=self.name, url=document_url, errors=str(e)[:600])
            return None


# =============================================================================
# 16. EXPORT - URL de-duplication, near-duplicate collapse, revisions, run summary, QA
# =============================================================================
REMOVED_COLUMNS = [
    "payer_name", "rule", "reason", "kept_document_url", "kept_title", "kept_confidence",
    "removed_document_url", "removed_title", "removed_confidence", "content_hash_sha256"
]


def _removed(keep: dict, r: dict, rule: str, reason: str = "") -> dict:
    return {
        "payer_name": r["payer_name"],
        "rule": rule,
        "reason": reason,
        "kept_document_url": keep["document_url"],
        "kept_title": keep.get("document_title", ""),
        "kept_confidence": keep.get("confidence_score", ""),
        "removed_document_url": r["document_url"],
        "removed_title": r.get("document_title", ""),
        "removed_confidence": r.get("confidence_score", ""),
        "content_hash_sha256": r.get("content_hash_sha256", "")
    }


def _completeness(r: dict) -> int:
    return sum(bool(r[c]) for c in ("policy_number", "effective_date", "last_updated_date"))


def _rank(r: dict) -> tuple:
    """Which copy to keep: on the payer's own domain, fetched OK, most complete, most confident, shortest URL."""
    return (r["_owner"], 0 if int(r["http_status"]) == 200 else 1, -_completeness(r), -float(r["confidence_score"]),
            len(r["document_url"]), r["document_url"])


def _add_note(row: dict, text: str) -> None:
    current = row["notes"].rstrip(".")
    row["notes"] = join_notes([current, text], limit=NOTES_MAX_CHARS + 300)


def _merge_into(keep: dict, others: list[dict]) -> None:
    group = [keep] + others
    if all(r["state_or_region"] != STATE_NATIONAL for r in group):
        merged = list(dict.fromkeys(s for r in group for s in r["state_or_region"].split(STATE_JOINER)))
        new = STATE_JOINER.join(merged)
        if new != keep["state_or_region"]:
            keep["state_or_region"] = new
            _add_note(keep, "state_or_region merged from identical copies")
    lobs = sorted({r["line_of_business"] for r in group} - {"Unknown", "Multiple"})
    if len(lobs) > 1 and keep["line_of_business"] != "Multiple":
        keep["line_of_business"] = "Multiple"
        keep["confidence_score"] = f"{max(0.05, float(keep['confidence_score']) - 0.05):.2f}"
        _add_note(keep, f"same file listed under {', '.join(lobs)}; line_of_business set to Multiple (inferred)")
    elif keep["line_of_business"] == "Unknown" and len(lobs) == 1:
        keep["line_of_business"] = lobs[0]
        _add_note(keep, "line_of_business taken from an identical copy's listing")
    for c in ("policy_number", "effective_date", "last_updated_date"):
        if not keep[c]:
            v = next((r[c] for r in others if r[c]), "")
            if v:
                keep[c] = v
    urls = [r["document_url"] for r in others]
    shown = ", ".join(urls[:2]) + (f" (+{len(urls) - 2} more, see near_duplicates_removed)" if len(urls) > 2 else "")
    _add_note(keep, f"same document also at {len(urls)} other URL(s): {shown}")


def _collapse(rows: list[dict], keyfn: Callable[[dict], Any], rule: str, reason: str, removed: list[dict]) -> list[dict]:
    groups: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        k = keyfn(r)
        if k:
            groups[k].append(r)
    drop: set[int] = set()
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=_rank)
        keep, others = g[0], g[1:]
        _merge_into(keep, others)
        for o in others:
            drop.add(id(o))
            removed.append(_removed(keep, o, rule, reason))
    return [r for r in rows if id(r) not in drop]


def _family(pn: str) -> str:
    pn = pn.strip().upper()
    m = re.fullmatch(r"20\d{2}([A-Z]\d{4})[A-Z0-9]{0,3}", pn)     # UHC 2025T0547R / 2026T0547S -> T0547
    return m.group(1) if m else pn


def _title_norm(t: str) -> str:
    t = re.sub(r"\b(?:19|20)\d{2}\b|\bv(?:ersion)?\s*\d+(?:\.\d+)*\b|\b(?:rev(?:ised|ision)?|updated?|archived?|current|new|draft)\b",
               " ", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


_ARCHIVE_RX = re.compile(r"(?<![a-z])(?:archived?|archives|prior[-_ ]?versions?|previous[-_ ]?versions?|historical|retired|"
                         r"obsolete|superseded|replaced)(?![a-z])", re.I)
_YEAR_RX = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")
_VER_RX = re.compile(r"(?<![a-z0-9])v(?:er(?:sion)?)?[ _.\-]?(\d{1,3})(?![0-9])", re.I)


def _is_archived(r: dict) -> bool:
    return bool(_ARCHIVE_RX.search(urlparse(r["document_url"]).path) or _ARCHIVE_RX.search(r["document_title"]))


def _year_hint(r: dict) -> int:
    """Latest year printed in the policy number (UHC 2025T0547R), the URL path or the title; 0 when none."""
    ys: list[int] = []
    m = re.match(r"(20[0-4]\d)[A-Z]", r["policy_number"] or "")
    if m:
        ys.append(int(m.group(1)))
    ys += [int(y) for y in _YEAR_RX.findall(urlparse(r["document_url"]).path)]
    ys += [int(y) for y in _YEAR_RX.findall(r["document_title"])]
    return max(ys) if ys else 0


def _ver_hint(r: dict) -> int:
    m = _VER_RX.search(urlparse(r["document_url"]).path.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else 0


def _revisions(rows: list[dict], removed: list[dict]) -> list[dict]:
    """Versions of one document: same payer + line of business + state + identity, different bytes.
    identity = policy-number family (UHC year/suffix stripped) when the number is a real id (shared by <= 3 distinct titles),
    else type + normalised title (years / version words removed).

    The LATEST version is kept (REVISION_POLICY = latest_only) and every older or archived version goes to the removal report.
    Order: the single most-populated date field of the group (effective_date, else last_updated_date); ties and undated copies
    are ordered by the year printed in the policy number / URL / title and by a v2/v3 number in the file name.
    Anything that cannot be ordered with confidence is KEPT and flagged for review (never dropped on a guess)."""
    titles_per_pn: dict[tuple[str, str], set] = defaultdict(set)
    for r in rows:
        if r["policy_number"]:
            titles_per_pn[(r["payer_name"], r["policy_number"].upper())].add(_title_norm(r["document_title"]))

    def ident(r: dict) -> Optional[tuple[str, str]]:
        pn = r["policy_number"]
        if pn and len(titles_per_pn[(r["payer_name"], pn.upper())]) <= 3:
            return "policy " + _family(pn), ""
        tn = _title_norm(r["document_title"])
        if len(tn) >= 8 and len(tn.split()) >= 2:
            return f"title '{tn}'", r["document_type"]
        return None

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if int(r["http_status"]) != 200:
            continue
        i = ident(r)
        if i:
            groups[(r["payer_name"], r["line_of_business"], r["state_or_region"], i[1], i[0])].append(r)
    drop: set[int] = set()
    for key, g in groups.items():
        if len(g) < 2:
            continue
        label = key[-1]
        live = [r for r in g if not _is_archived(r)]
        pool = live or g
        n_eff = sum(bool(r["effective_date"]) for r in pool)
        n_upd = sum(bool(r["last_updated_date"]) for r in pool)
        fld = "effective_date" if n_eff >= n_upd and n_eff > 0 else ("last_updated_date" if n_upd > 0 else "")
        vkey = lambda r: ((r[fld] if fld else "") or "", _year_hint(r), _ver_hint(r))
        dated = [r for r in pool if fld and r[fld]]
        ranked = sorted(dated or pool, key=vkey, reverse=True)
        top = vkey(ranked[0])
        tied = [r for r in ranked if vkey(r) == top] if (dated or top[1:] != (0, 0)) else []
        latest = tied[0] if len(tied) == 1 else None
        ref = latest or (tied[0] if tied else pool[0])
        n_dropped = 0
        for r in g:
            if r in live or not live:
                has_date = bool(fld and r[fld])
                if latest is not None and r is latest:
                    status, older = "latest", False
                elif latest is None and r in tied:
                    status, older = "same version date as another copy - possible duplicate, review", False
                elif tied and has_date and vkey(r) < top:
                    status, older = f"superseded by {ref['document_url']}", True
                elif tied and not has_date and fld and _year_hint(r) and _year_hint(r) < top[1]:
                    status, older = f"superseded by {ref['document_url']} (older year in the policy number / URL / title)", True
                elif tied and not dated and vkey(r) < top:
                    status, older = f"superseded by {ref['document_url']} (older year / version in the policy number / URL)", True
                else:
                    status, older = f"no {fld or 'date'} printed - order unknown, review", False
            else:
                status, older = f"archived copy; current version at {ref['document_url']}", True
            if REVISION_POLICY == "latest_only" and older:
                drop.add(id(r))
                n_dropped += 1
                if r in live or not live:
                    reason_text = (f"Superseded by a newer revision of {label}. The kept document is dated {top[0] or 'n/a'} "
                                   f"(field: {fld or 'year/version hints'}). {status}.")
                else:
                    reason_text = f"Archived / historical copy of {label}; the current version is kept. {status}."
                removed.append(_removed(ref, r, "superseded_revision", reason_text))
                continue
            _add_note(r, f"revision group ({len(g)} versions of {label}, compared on {fld or 'year/version hints'}): {status}")
        if n_dropped:
            _add_note(ref, f"latest of {len(g)} versions; {n_dropped} older version(s) in near_duplicates_removed")
    return [r for r in rows if id(r) not in drop]


def build_rows(db: StateDB) -> tuple[list[dict], list[dict]]:
    """All accepted rows of every payer after the near-duplicate rule. Returns (rows, removed)."""
    home: dict[str, set] = {}
    rows: list[dict] = []
    for rec in db.con.execute("SELECT payer, row_json, decision FROM documents WHERE accepted=1"):
        row = json.loads(rec["row_json"])
        dec = json.loads(rec["decision"] or "{}")
        p = row["payer_name"]
        if p not in home:
            home[p] = set(db.get(p, "home_domains", []))
        row["_fp"] = dec.get("text_fp", "") or ""
        row["_canon"] = dec.get("canonical_key", "") or ""
        row["_owner"] = 0 if reg_domain(urlparse(row["document_url"]).hostname or "") in home[p] else 1
        rows.append(row)
    removed: list[dict] = []
    # 1) one row per URL key across the WHOLE file (http/https, www, slash, query order, cache-busters)
    rows.sort(key=_rank)
    seen: dict[str, dict] = {}
    kept: list[dict] = []
    for r in rows:
        k = url_key(r["document_url"])
        if k in seen:
            keep = seen[k]
            rule_name = "same_url_key" if keep["payer_name"] == r["payer_name"] else "same_url_key_cross_payer"
            reason_text = "Identical URL path/query (ignoring tracking parameters, case, and www). Kept the highest-ranked version."
            removed.append(_removed(keep, r, rule_name, reason_text))
            continue
        seen[k] = r
        kept.append(r)
    rows = kept
    ok = lambda r: r["requires_auth"] == "N" and bool(r["content_hash_sha256"])
    # 2) same bytes at several URLs, 3) same normalised text, 4) same canonical URL
    rows = _collapse(rows, lambda r: (r["payer_name"], r["content_hash_sha256"]) if ok(r) else None, "identical_sha256",
                     "Identical file content (exact SHA-256 hash match). Kept the version with better ownership/status.", removed)
    rows = _collapse(rows, lambda r: (r["payer_name"], r["_fp"]) if ok(r) and r["_fp"] else None, "identical_text",
                     "Identical extracted text content (near-duplicate). Kept the primary version.", removed)
    rows = _collapse(rows, lambda r: (r["payer_name"], r["_canon"]) if r["_canon"] else None, "same_canonical_url",
                     "Both URLs share the same HTML <link rel='canonical'> tag. Kept the canonical version.", removed)

    # 5) dated revisions
    rows = _revisions(rows, removed)
    for r in rows:
        for k in ("_fp", "_canon", "_owner"):
            r.pop(k, None)
        r["notes"] = join_notes([r["notes"].rstrip(".")], limit=NOTES_MAX_CHARS + 300)
    rows.sort(key=lambda r: (r["payer_name"], r["document_type"], r["document_title"].lower(), r["document_url"]))
    return rows, removed


def qa_check(rows: list[dict]) -> dict:
    keys = [url_key(r["document_url"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate document_url (url_key) in output"
    assert len({r["document_url"] for r in rows}) == len(rows), "duplicate document_url in output"
    by_hash: dict[tuple, int] = defaultdict(int)
    by_meta: dict[tuple, int] = defaultdict(int)
    for r in rows:
        if r["requires_auth"] == "N" and r["content_hash_sha256"]:
            by_hash[(r["payer_name"], r["content_hash_sha256"])] += 1
        by_meta[(r["payer_name"], r["document_title"].lower(), r["line_of_business"], r["state_or_region"], r["document_type"])] += 1
    hash_dupes = sum(1 for v in by_hash.values() if v > 1)
    meta_dupes = sum(v for v in by_meta.values() if v > 1)
    for r in rows:   # re-validate every row after the merge step
        PolicyRow(**{**r, "confidence_score": float(r["confidence_score"])})
    res = {"rows": len(rows), "same_hash_groups": hash_dupes, "rows_sharing_title_lob_state_type": meta_dupes}
    log(logging.INFO if not hash_dupes else logging.WARNING, "qa_check", **res)
    return res


def summarize(db: StateDB, payer: str, rows: list[dict], removed: list[dict]) -> dict:
    c = db.status_counts(payer)
    g = lambda kinds, states: sum(c.get((k, s), 0) for k in kinds for s in states)
    D = ("doc", "maybe_doc")
    prow = [r for r in rows if r["payer_name"] == payer]
    dups = [x for x in removed if x["payer_name"] == payer]
    found = db.accepted_count(payer)
    routes: dict[str, int] = {}
    for rr in db.con.execute("SELECT route, COUNT(*) n FROM documents WHERE payer=? AND accepted=1 GROUP BY route", (payer,)):
        routes[rr["route"] or "seed"] = rr["n"]
    types: dict[str, int] = defaultdict(int)
    for r in prow:
        types[r["document_type"]] += 1
    skipped_links = db.con.execute("SELECT COUNT(*) FROM skipped_links WHERE payer=?", (payer,)).fetchone()[0]
    ok = found - len(dups) == len(prow)
    return {
        "payer_name": payer,
        "attempted": g(D, ("accepted", "rejected", "blocked", "failed")),
        "found": found,
        "failed": g(D, ("failed",)),
        "skipped": g(D, ("rejected", "skipped_robots", "skipped_budget")),
        "skipped_not_policy": g(D, ("rejected",)),
        "skipped_robots": g(D, ("skipped_robots",)),
        "skipped_budget": g(D, ("skipped_budget",)),
        "blocked": g(D, ("blocked",)),
        "duplicates_removed": len(dups),
        "rows_in_output": len(prow),
        "rows_http_200": sum(1 for r in prow if int(r["http_status"]) == 200),
        "rows_non_200": sum(1 for r in prow if int(r["http_status"]) != 200),
        "rows_requires_auth": sum(1 for r in prow if r["requires_auth"] == "Y"),
        "rows_confidence_below_0_70": sum(1 for r in prow if float(r["confidence_score"]) < 0.70),
        "reconciliation": f"found {found} - duplicates_removed {len(dups)} = rows_in_output {len(prow)}: {'OK' if ok else 'MISMATCH'}",
        "doc_links_skipped_low_score": skipped_links,
        "pages_crawled": g(("page",), ("done",)),
        "pages_blocked": g(("page",), ("blocked",)),
        "pages_failed": g(("page",), ("failed",)),
        "pages_skipped_robots": g(("page",), ("skipped_robots",)),
        "pending": g(("page", "doc", "maybe_doc"), ("pending", "in_progress")),
        "found_by_route": json.dumps(routes, sort_keys=True),
        "sitemap_share_pct": round(100 * routes.get("sitemap", 0) / found, 1) if found else 0.0,
        "rows_provider_manual": types.get("provider_manual", 0), "rows_bulletin": types.get("bulletin", 0),
        "rows_pa_form": types.get("pa_form", 0), "rows_pa_list": types.get("pa_list", 0),
        "rows_by_document_type": json.dumps(dict(sorted(types.items()))),
        "related_domains": json.dumps(db.get(payer, "related_domains", [])),
        "summary_timestamp_utc": utc_now_iso(),
    }


def _write_table(rows: list[dict], removed: list[dict], summaries: list[dict], stem: str) -> None:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    
    # 1. Write CSV using built-in csv module
    csv_path = os.path.join(OUTPUT_DIR, f"{stem}.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for r in rows:
            row_out = {}
            for c in COLUMNS:
                val = r.get(c, "")
                if c in ("http_status", "file_size_bytes"):
                    row_out[c] = int(val) if val != "" else ""
                else:
                    row_out[c] = str(val) if val is not None else ""
            writer.writerow(row_out)
            
    # 2. Write Excel using openpyxl (Pure Python, bypasses DLL blocks)
    xlsx_path = os.path.join(OUTPUT_DIR, f"{stem}.xlsx")
    from openpyxl.cell import WriteOnlyCell
    wb = Workbook(write_only=True)          # streaming writer: no per-cell objects kept in memory

    # Sheet 1: documents
    ws_docs = wb.create_sheet("documents")
    ws_docs.freeze_panes = "A2"
    for i, col in enumerate(COLUMNS, start=1):
        ws_docs.column_dimensions[get_column_letter(i)].width = min(60, max(12, len(col) + 2))
    ws_docs.append(COLUMNS)
    for r in rows:
        row_out = []
        for c in COLUMNS:
            val = r.get(c, "")
            if c in ("http_status", "file_size_bytes"):
                row_out.append(int(val) if val != "" else "")
            else:
                cell = WriteOnlyCell(ws_docs, value=str(val) if val is not None else "")
                cell.number_format = "@"      # text columns stay text (policy numbers keep leading zeros)
                row_out.append(cell)
        ws_docs.append(row_out)

    # Sheet 2: run_summary
    if summaries:
        ws_sum = wb.create_sheet("run_summary")
        sum_cols = list(summaries[0].keys())
        ws_sum.append(sum_cols)
        for s in summaries:
            ws_sum.append([str(s.get(c, "")) for c in sum_cols])

    # Sheet 3: near_duplicates_removed
    if removed:
        ws_rem = wb.create_sheet("near_duplicates_removed")
        ws_rem.append(REMOVED_COLUMNS)
        for r in removed:
            ws_rem.append([str(r.get(c, "")) for c in REMOVED_COLUMNS])

    wb.save(xlsx_path)
    log(logging.INFO, "exported", file=xlsx_path, rows=len(rows))



def _write_csv(data: list[dict], columns: list[str], filepath: str) -> None:
    if not data or not columns:
        return
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in data:
            writer.writerow({c: row.get(c, "") for c in columns})


def _write_type_keyword_audit(db: StateDB) -> None:
    """type_keyword_audit.csv: every candidate whose link text / title / URL carries a provider-manual, bulletin or
    prior-authorization keyword, and what happened to it. If these types are missing from the output, this file says where
    each candidate was lost (skipped at the link stage, rejected after download, or accepted and as which type)."""
    out: list[dict] = []
    for r in db.con.execute("SELECT payer, url, anchor, heading, score, reason FROM skipped_links"):
        kt, kw = type_keyword_hit(f"{r['anchor'] or ''} {url_words(r['url'])}")
        if not kt:
            kt, kw = type_keyword_hit(r["heading"] or "")
        if kt:
            out.append({"payer_name": r["payer"], "document_url": r["url"], "keyword_type": kt, "matched_keyword": kw,
                        "outcome": "skipped at the link stage (never downloaded)", "document_type": "",
                        "link_score": r["score"], "content_score": "", "reason": r["reason"]})
    for r in db.con.execute("SELECT payer, document_url, accepted, row_json, decision FROM documents"):
        row, dec = json.loads(r["row_json"] or "{}"), json.loads(r["decision"] or "{}")
        title = row.get("document_title", "") or row.get("anchor", "")
        kt, kw = type_keyword_hit(f"{title} {row.get('anchor', '')} {url_words(r['document_url'])}")
        if not kt and row.get("document_type") not in ("provider_manual", "bulletin", "pa_form", "pa_list"):
            continue
        out.append({"payer_name": r["payer"], "document_url": r["document_url"], "keyword_type": kt or "", "matched_keyword": kw,
                    "outcome": "accepted" if r["accepted"] else "rejected after download",
                    "document_type": row.get("document_type", ""), "link_score": dec.get("link_score", ""),
                    "content_score": dec.get("content_score", ""),
                    "reason": dec.get("reason", "") if not r["accepted"] else (dec.get("type_hint") or "")})
    if out:
        _write_csv(out, list(out[0].keys()), os.path.join(OUTPUT_DIR, "type_keyword_audit.csv"))


def export(db: StateDB) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, removed = build_rows(db)
    qa_check(rows)
    payers = sorted(set(db.payers()) | {r["payer_name"] for r in rows})
    summaries = [summarize(db, p, rows, removed) for p in payers]
    
    _write_table(rows, removed, summaries, "output")
    for p, s in zip(payers, summaries):
        _write_table([r for r in rows if r["payer_name"] == p], [x for x in removed if x["payer_name"] == p], [s],
                     f"policy_documents_{_safe(p)}")
                     
    # Export auxiliary CSVs using the built-in csv helper
    _write_csv(summaries, list(summaries[0].keys()) if summaries else [], os.path.join(OUTPUT_DIR, "run_summary.csv"))
    _write_csv(removed, REMOVED_COLUMNS, os.path.join(OUTPUT_DIR, "near_duplicates_removed.csv"))
    
    rej = []
    for r in db.con.execute("SELECT payer, document_url, row_json, decision FROM documents WHERE accepted=0"):
        row = json.loads(r["row_json"] or "{}")
        dec = json.loads(r["decision"] or "{}")

        llm_data = dec.get("llm") or {}
        if not isinstance(llm_data, dict):
            llm_data = {}

        rej.append({
            "payer_name": r["payer"],
            "document_url": r["document_url"],
            "document_title": row.get("document_title", ""),
            "file_type": row.get("file_type", ""),
            "link_score": dec.get("link_score", ""),
            "content_score": dec.get("content_score", ""),
            "rejection_reason": dec.get("reason", "Unknown"),
            "llm_reason": llm_data.get("reason", ""),
            "content_signals": ", ".join(dec.get("content_signals", [])),
            "link_signals": ", ".join(dec.get("link_signals", []))
        })
    if rej:
        _write_csv(rej, list(rej[0].keys()), os.path.join(OUTPUT_DIR, "rejected_candidates.csv"))
        
    _write_type_keyword_audit(db)

    blocked = [dict(r) for r in db.con.execute("SELECT * FROM blocked_pages")]
    if blocked:
        _write_csv(blocked, list(blocked[0].keys()), os.path.join(OUTPUT_DIR, "blocked_pages.csv"))
        
    sk = [dict(r) for r in db.con.execute(
        "SELECT s.* FROM skipped_links s LEFT JOIN frontier f ON f.payer=s.payer AND f.key=s.key WHERE f.key IS NULL "
        "ORDER BY s.payer, s.source_page_url, s.score DESC")]
    if sk:
        _write_csv(sk, list(sk[0].keys()), os.path.join(OUTPUT_DIR, "skipped_doc_links.csv"))
        
    for s in summaries:
        log(logging.INFO, "run_summary", **{k: s[k] for k in ("payer_name", "attempted", "found", "failed", "skipped",
                                                              "blocked", "duplicates_removed", "rows_in_output", "reconciliation")})

# =============================================================================
# 17. MAIN
# =============================================================================
def load_registry() -> list[dict]:
    reg = [dict(p) for p in PAYER_REGISTRY]
    if os.path.exists(SEED_LIST_CSV):
        with open(SEED_LIST_CSV, newline="", encoding="utf-8-sig") as fh:
            for rec in csv.DictReader(fh):
                low = {re.sub(r"[^a-z]", "", k.lower()): (v or "").strip() for k, v in rec.items() if k}
                name = low.get("payer") or low.get("payername") or low.get("name") or ""
                host = low.get("hinthost") or low.get("host") or low.get("homepage") or low.get("url") or ""
                if not host:
                    continue
                host_key = reg_domain(urlparse(host if "://" in host else f"https://{host}").hostname or host)
                match = next((p for p in reg if reg_domain(p["hint_host"]) == host_key), None)
                if match:
                    match["payer_name"] = name or match["payer_name"]
                    match["hint_host"] = host
                elif name:
                    reg.append({"payer_name": name, "payer_alias": "", "hint_host": host, "footprint_state": "",
                                "affiliated_domains": [host_key], "brand_terms": [name]})
        log(logging.INFO, "seed_list_loaded", path=SEED_LIST_CSV, payers=len(reg))
    return reg


def resolve_payer(homepage: str, registry: list[dict]) -> dict:
    host = urlparse(homepage if "://" in homepage else f"https://{homepage}").hostname or homepage
    dom = reg_domain(host)
    match = next((p for p in registry if reg_domain(p["hint_host"]) == dom), None)
    payer = dict(match) if match else {"payer_name": PAYER_NAME or dom.split(".")[0].title(), "payer_alias": "",
                                        "hint_host": host, "footprint_state": "", "affiliated_domains": [dom],
                                        "brand_terms": [PAYER_NAME or dom.split(".")[0]]}
    if PAYER_NAME:
        payer["payer_name"] = PAYER_NAME
    payer["homepage"] = homepage if "://" in homepage else f"https://{homepage}"
    payer["is_current"] = True
    return payer


def _crawl_payer_worker(i: int, payer: dict, ui: ProgressUI, shared_fetcher: Fetcher) -> None:
    """One payer in its own thread: own SQLite connection, own browser, own LLM client and own fetch pool; politeness
    clock, robots cache and HTTP client are shared. The crawl logic itself is the unchanged PayerCrawler."""
    db = StateDB(STATE_DB)
    fetcher = Fetcher(share_with=shared_fetcher)
    browser = Browser()
    llm = LLMClassifier(db)
    try:
        ui.payer_started(i, payer["payer_name"])
        llm.calls = 0
        try:
            PayerCrawler(payer, db, fetcher, browser, llm, ui).run()
        except KeyboardInterrupt:
            log(logging.WARNING, "payer_interrupted", payer=payer["payer_name"], hint="re-run the script to resume")
        except Exception as e:
            log(logging.ERROR, "payer_failed", payer=payer["payer_name"], error=repr(e)[:300], trace=traceback.format_exc()[-1500:])
        ui.payer_finished(i)
    finally:
        browser.close()
        fetcher.close()
        db.con.close()


def main() -> None:
    ui = ProgressUI()
    setup_logging(ui)
    load_schema_dictionary()
    registry = load_registry()
    if RUN_ALL_PAYERS:
        payers = [dict(p, homepage=p["hint_host"] if "://" in p["hint_host"] else f"https://{p['hint_host']}") for p in registry]
    else:
        payers = [resolve_payer(HOMEPAGE_URL, registry)]
    db = StateDB(STATE_DB)
    fetcher = Fetcher()
    browser = Browser()
    llm = LLMClassifier(db)
    parallel = PARALLEL_PAYERS > 1 and len(payers) > 1
    if parallel:
        ui.use_rich = False          # one live bar cannot show several payers; plain per-payer status lines are used instead
    ui.start(len(payers))
    try:
        if parallel:
            log(logging.INFO, "parallel_payers", workers=min(PARALLEL_PAYERS, len(payers)), payers=len(payers))
            pool = ThreadPoolExecutor(max_workers=min(PARALLEL_PAYERS, len(payers)), thread_name_prefix="payer")
            futs = [pool.submit(_crawl_payer_worker, i, payer, ui, fetcher) for i, payer in enumerate(payers, start=1)]
            try:
                for f in futs:
                    f.result()
            except KeyboardInterrupt:
                STOP.set()           # workers stop at their next checkpoint; the current item finishes first
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            pool.shutdown(wait=True)
            payers = []              # nothing left for the sequential loop below
        for i, payer in enumerate(payers, start=1):
            ui.payer_started(i, payer["payer_name"])
            llm.calls = 0
            try:
                PayerCrawler(payer, db, fetcher, browser, llm, ui).run()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(logging.ERROR, "payer_failed", payer=payer["payer_name"], error=repr(e)[:300], trace=traceback.format_exc()[-1500:])
            ui.payer_finished(i)
    except KeyboardInterrupt:
        log(logging.WARNING, "interrupted", hint="re-run the script to resume from the checkpoint")
    finally:
        try:
            ui.phase("export")
            export(db)
        except Exception as e:
            log(logging.ERROR, "export_failed", error=repr(e)[:300], trace=traceback.format_exc()[-1500:])
        finally:
            ui.stop()
            browser.close()
            fetcher.close()
            db.con.close()


if __name__ == "__main__":
    main()
