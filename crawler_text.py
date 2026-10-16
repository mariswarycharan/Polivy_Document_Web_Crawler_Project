#!/usr/bin/env python3
"""Module 2/5 - text rules: URL/text helpers, keyword knowledge (Gate 0/1, doc type, LOB, state, content fingerprint) and metadata extraction.

Part of the Payer Policy Document Discovery Crawler (v2). Logic is unchanged from the single-file version."""
from __future__ import annotations

import threading
from functools import lru_cache

import hashlib
import re
import tldextract
from collections import defaultdict
from datetime import date, datetime, timezone
from dateutil import parser as dateparser
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from crawler_config import (CDN_HOST_SUFFIXES, EXCLUDED_DOC_TYPES, INFER_STATE_FROM_PAYER_FOOTPRINT, NOTES_MAX_CHARS, PAYER_REGISTRY, STATE_JOINER, STATE_NATIONAL, TEXT_FP_MIN_CHARS)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from crawler_io import Inspection


# =============================================================================
# 5. GENERIC HELPERS
# =============================================================================
_TLD = tldextract.TLDExtract(suffix_list_urls=())  # bundled public-suffix snapshot, no network


@lru_cache(maxsize=65536)
def reg_domain(host: str) -> str:
    e = _TLD(host or "")
    return f"{e.domain}.{e.suffix}".lower() if e.suffix else (host or "").lower()


def host_has_suffix(host: str, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_cdn_host(host: str) -> bool:
    return host_has_suffix(host, CDN_HOST_SUFFIXES)


_S3_PATH_HOST = re.compile(r"^s3[.-][a-z0-9-]*\.?amazonaws\.com$|^s3\.amazonaws\.com$")


def cdn_tenant(url: str) -> str:
    """Who owns the files on a shared host: the host itself (account / distribution / bucket-in-hostname), or host + first
    path segment where one host serves many tenants by path (storage.googleapis.com/<bucket>, s3.amazonaws.com/<bucket>,
    <tenant>.sharepoint.com/sites/<site>). '' when the URL is not on a CDN host."""
    pu = urlparse(url)
    host = (pu.hostname or "").lower()
    if not is_cdn_host(host):
        return ""
    segs = [s for s in pu.path.split("/") if s]
    if host == "storage.googleapis.com" or _S3_PATH_HOST.match(host):
        return f"{host}/{segs[0].lower()}" if segs else host
    if host.endswith("sharepoint.com") and len(segs) >= 2 and segs[0].lower() in ("sites", "teams"):
        return f"{host}/{segs[0].lower()}/{segs[1].lower()}"
    return host


def clean_text(s: Optional[str], limit: int = 0) -> str:
    s = re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()
    return s[:limit].rstrip() if limit and len(s) > limit else s


def clean_text_keep_lines(s: str) -> str:
    return "\n".join(clean_text(l) for l in (s or "").splitlines() if clean_text(l))


# tracking, session and cache-buster parameters are removed from every URL
_TRACKING = re.compile(
    r"^(?:utm_\w+|gclid|gbraid|wbraid|dclid|fbclid|msclkid|mc_cid|mc_eid|_ga|_gl|_hsenc|_hsmi|hsctatracking|mkt_tok|"
    r"jsessionid|phpsessid|aspsessionid\w*|sessionid|cfid|cftoken|"
    r"ts|timestamp|cb|cachebust(?:er)?|cache|nocache|rnd|rand|random|_)$", re.I)
_VERSION_BUSTER_KEY = re.compile(r"^(?:v|ver)$", re.I)
_VERSION_BUSTER_VAL = re.compile(r"^(?:\d{6,}|[0-9a-f]{8,})$", re.I)   # ?v=1699999999 / ?ver=a3f9c2e1 are cache-busters


def _drop_param(k: str, v: str) -> bool:
    return bool(_TRACKING.match(k)) or (bool(_VERSION_BUSTER_KEY.match(k)) and bool(_VERSION_BUSTER_VAL.match(v or "")))


@lru_cache(maxsize=262144)
def normalize_url(url: str, base: str = "") -> str:
    """Absolute, fragment-free, tracking/session/cache-buster-free URL with lower-case host. '' if not http(s)."""
    if not url:
        return ""
    url = url.strip().strip("'\"")
    if url.startswith(("mailto:", "tel:", "javascript:", "data:", "#", "sms:", "fax:", "blob:")):
        return ""
    try:
        if base:
            url = urljoin(base, url)
        p = urlparse(url)
        port = p.port
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    host = p.hostname.lower() if p.hostname else ""
    if not host or " " in host:
        return ""
    port = port if port and not ((p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)) else None
    netloc = f"{host}:{port}" if port else host
    path = re.sub(r";(?:jsessionid|phpsessid|aspsessionid[a-z0-9]*|sessionid|cfid|cftoken)=[^/?#]*", "", p.path or "/", flags=re.I)
    path = re.sub(r"/{2,}", "/", path) or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _drop_param(k, v)], doseq=True)
    return urlunparse((p.scheme, netloc, path, "", query, ""))


@lru_cache(maxsize=262144)
def url_key(url: str) -> str:
    """De-duplication key: scheme-less, www-less, trailing-slash-less, sorted query, decoded path."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = unquote(p.path or "/").rstrip("/") or "/"
    # Lowercase keys and values to treat ?id=ABC and ?id=abc as the same URL
    q = urlencode(sorted((k.lower(), v.lower()) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _drop_param(k, v)))
    return f"{host}{port}{path}" + (f"?{q}" if q else "")


@lru_cache(maxsize=262144)
def url_ext(url: str) -> str:
    path = unquote(urlparse(url).path).lower()
    m = re.search(r"\.([a-z0-9]{2,5})$", path)
    return m.group(1) if m else ""


# concatenated path words ("/medicalpolicies/", "/PriorAuthList") -> readable phrases
_SQUASHED_STEMS = [
    ("medicalpolic", "medical policy"), ("coveragepolic", "coverage policy"), ("clinicalpolic", "clinical policy"),
    ("pharmacypolic", "pharmacy policy"), ("drugpolic", "drug policy"), ("paymentpolic", "payment policy"),
    ("reimbursementpolic", "reimbursement policy"), ("priorauth", "prior authorization"), ("preauth", "prior authorization"),
    ("precert", "precertification"), ("formular", "formulary"), ("druglist", "drug list"), ("preferreddrug", "preferred drug"),
    ("clinicalguideline", "clinical guideline"), ("medicalnecessity", "medical necessity guidelines"),
    ("clinicalcriteria", "clinical criteria"), ("coverageguideline", "coverage guideline"),
    ("providermanual", "provider manual"), ("policybulletin", "policy bulletin"), ("policyupdate", "policy update"),
]


@lru_cache(maxsize=262144)
def url_words(url: str) -> str:
    """Words of the URL path + query. camelCase is split BEFORE lower-casing; squashed stems are expanded."""
    p = urlparse(url)
    raw = unquote(f"{p.path} {p.query}")
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)          # coveragePolicies -> coverage Policies
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", raw)        # PDFPolicy -> PDF Policy
    words = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    compact = words.replace(" ", "")
    extra = [phrase for stem, phrase in _SQUASHED_STEMS if stem in compact and phrase not in words]
    return clean_text(" ".join([words] + extra))


def short_url(url: str, base_host: str) -> str:
    p = urlparse(url)
    if (p.hostname or "") == base_host:
        return (p.path or "/") + (f"?{p.query}" if p.query else "")
    return url


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def join_notes(parts: list[str], limit: int = NOTES_MAX_CHARS) -> str:
    out: list[str] = []
    for p in parts:
        p = clean_text(p).rstrip(".;")
        if p and p not in out:
            out.append(p)
    text = "; ".join(out)
    if text:
        text += "."
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


# =============================================================================
# 6. KEYWORD KNOWLEDGE (Gate 0 / Gate 1 / document type / line of business / state)
# =============================================================================
JUNK_EXTS = {"jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "bmp", "tif", "tiff", "css", "js", "mjs",
             "woff", "woff2", "ttf", "eot", "otf", "mp4", "mp3", "mov", "avi", "wav", "webm", "m4a", "zip",
             "gz", "rar", "7z", "exe", "dmg", "ics", "vcf", "json", "rss", "atom", "map", "swf"}
DOC_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "rtf", "txt"}
PAGE_EXTS = {"html", "htm", "aspx", "asp", "php", "jsp", "cfm", "shtml", "page", "xhtml"}
MAYBE_DOC_HINTS = ("/-/media/", "/content/dam/", "/documents/", "/document/", "/download", "getdocument",
                   "/files/", "/file/", "/dam/", ".ashx", "blob.core.windows.net", "/pdfs/", "/pdf/",
                   "documentid=", "docid=", "fileid=", "getfile", "viewdocument", "/attachments/")

THIRD_PARTY_BLOCKLIST = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "youtu.be",
    "pinterest.com", "tiktok.com", "google.com", "googleapis.com", "gstatic.com", "apple.com",
    "microsoft.com", "adobe.com", "doubleclick.net", "googletagmanager.com", "bing.com", "yahoo.com",
    "vimeo.com", "flickr.com", "wikipedia.org", "glassdoor.com", "indeed.com", "medium.com",
    "cms.gov", "medicare.gov", "healthcare.gov", "hhs.gov", "fda.gov", "cdc.gov", "nih.gov",
    "addtoany.com", "sharethis.com", "onetrust.com", "cookielaw.org", "qualtrics.com", "zoom.us",
    # industry associations and health-news sites: they write ABOUT payer policies but do not publish them
    "bcbs.com", "kff.org", "healthaffairs.org", "fiercehealthcare.com", "modernhealthcare.com",
    "beckershospitalreview.com", "beckerspayer.com", "statnews.com", "ama-assn.org", "aha.org", "prnewswire.com",
    "businesswire.com", "globenewswire.com",
}

POSITIVE_TERMS: list[tuple[str, float]] = [
    (r"medical (?:coverage )?polic(?:y|ies)", 6), (r"coverage polic(?:y|ies)", 6),
    (r"clinical (?:coverage |payment |pharmacy )?polic(?:y|ies)", 6), (r"clinical policy bulletins?", 6),
    (r"coverage determination guidelines?", 6), (r"medical necessity (?:criteria|guidelines?)", 5),
    (r"clinical criteria|coverage criteria|coverage guidelines?|clinical (?:utilization management |um )?guidelines?", 5),
    (r"utilization (?:management|review)", 3),
    (r"prior[\s-]?auth(?:orization|orizations)?|pre[\s-]?auth(?:orization)?|pre[\s-]?cert(?:ification)?|precertification|"
     r"pre[\s-]?service review|pre[\s-]?determination", 5),
    (r"formular(?:y|ies)", 6), (r"drug lists?|medication lists?", 6), (r"preferred drug", 5), (r"\bpdl\b", 4),
    (r"(?:drug|pharmacy|medication|specialty pharmacy|medical benefit drug|injectable drug) polic(?:y|ies)", 6),
    (r"step therapy|quantity limits?", 4),
    (r"(?:reimbursement|payment) polic(?:y|ies)", 5),
    (r"polic(?:y|ies),? (?:and|&) (?:guidelines|procedures|protocols|criteria)", 5),
    # --- added: policy hubs / numbering schemes / criteria / pharmacy edits / PA forms
    (r"polic(?:y|ies) (?:search|finder|library|index|lookup|center|manual)|search (?:medical |coverage |clinical )?polic(?:y|ies)", 4),
    (r"archived (?:medical |coverage |clinical )?polic(?:y|ies)|retired polic(?:y|ies)", 3),
    (r"medical necessity|experimental (?:or|/|and) investigational|investigational", 3),
    (r"(?:prior authorization|pa|drug coverage|coverage approval|approval) criteria|criteria for (?:coverage|approval|use)", 5),
    (r"(?:clinical|coverage|medical|drug) (?:review )?criteria", 4),
    (r"\b(?:cg|ocg)[\s-][a-z]{2,6}[\s-]\d+\b|\b(?:med|surg|drug|rad|lab|gene|dme|rx|beh|ther|trans|ocg)[\s.]\d{5}\b|\bcpb\b", 4),
    (r"site[\s-]of[\s-]care (?:polic(?:y|ies)|guidelines?|criteria)", 5),
    (r"level[\s-]of[\s-]care (?:guidelines?|criteria)|behavioral health (?:clinical|coverage|medical necessity) (?:polic(?:y|ies)|guidelines?|criteria)", 5),
    (r"(?:medical )?injectables?|specialty (?:drugs?|medications?)|biosimilars?|clinical edits?|drug exclusions?|excluded drugs?|exclusion lists?", 4),
    (r"(?:prior authorization|precertification|pre-?authorization) (?:code )?(?:list|lookup|requirements?|criteria|matrix)", 5),
    (r"(?:services|codes|procedures|drugs|medications) (?:that )?(?:require|requiring) (?:prior auth\w*|precert\w*|pre-?authorization)", 5),
    (r"(?:formulary|step therapy|quantity limit|prior authorization|coverage|tier) (?:exceptions?|overrides?)", 4),
    (r"(?:medication|drug|pharmacy|rx) (?:prior authorization|pa|exception|coverage determination) (?:request|forms?)", 5),
    (r"(?:formulary|drug list|preferred drug list|pdl) (?:updates?|changes|additions|deletions|exclusions?)", 3),
    (r"(?:authorization|request|exception|precertification|pa|referral) forms?", 4),
    (r"medication request|drug request|coverage determination", 3),
    (r"\bpolicies\b", 2), (r"\bpolicy\b", 1), (r"\bcriteria\b", 2), (r"\bguidelines?\b", 1),
]
NAV_TERMS: list[tuple[str, float]] = [
    (r"for providers|provider resources|provider center|provider portal resources", 3),
    (r"\bproviders?\b|professionals?|practitioners?|clinicians?", 2), (r"\bpharmacy\b|\bpharmacists?\b", 2.5),
    (r"\bresources?\b|\btools\b|\blibrary\b", 1), (r"\bforms?\b", 1.5), (r"\bdrugs?\b|prescriptions?", 1.5),
    (r"\bclinical\b", 1.5), (r"\bplans?\b", 0.3),
    (r"polic(?:y|ies) (?:and|&) (?:guidelines|procedures|criteria|coverage)|coverage (?:and|&) (?:polic(?:y|ies)|criteria|guidelines)", 3),
    (r"utilization management|medical management|prior auth|precert|authorizations?", 2),
    (r"pharmacy (?:benefits?|resources|services|programs?)|clinical (?:resources|tools|programs)|medical (?:resources|policies)", 2),
    (r"forms (?:and|&) (?:documents|resources|guides)|documents (?:and|&) forms|document library", 2),
    (r"polic(?:y|ies) (?:updates?|notifications?|changes)", 2),     # such pages often list the real policies; they are followed
]
NEGATIVE_TERMS: list[tuple[str, float]] = [
    (r"privacy|hipaa notice|notice of privacy practices", -10), (r"terms (?:of use|and conditions|of service)", -10),
    (r"cookies?", -10), (r"non-?discrimination|language assistance|taglines?|accessibility", -6),
    (r"careers?|\bjobs?\b|job openings", -10), (r"investors?|shareholders?|annual report|sec filings|\besg\b|sustainability", -10),
    (r"press releases?|newsroom|media (?:center|room)|news releases?", -6), (r"\bnews\b", -2), (r"\bblogs?\b|\barticles?\b", -4),
    (r"\bevents?\b|webinars?", -3), (r"log ?in|sign ?in|sign ?up|register|my account|forgot password|reset password", -6),
    (r"member handbook|evidence of coverage|summary of benefits|\bsbc\b|annual notice of change|benefit summary|plan overview", -5),
    (r"brochures?|flyers?|postcards?|infographics?", -5), (r"newsletters?", -3), (r"foundation|donat(?:e|ion)", -6),
    (r"find a (?:doctor|provider|pharmacy|dentist)|provider directory|find care|doctor finder", -5),
    (r"contact us|about us|leadership|board of directors|our story|mission", -5),
    (r"transparency in coverage|machine[\s-]readable|\bmrf\b|price transparency", -6),
    (r"facebook|twitter|linkedin|instagram|youtube", -10), (r"\bsitemap\b", -2), (r"\bespa[nñ]ol\b|\bspanish\b", -0.5),
    (r"shop (?:for )?plans?|get a quote|enroll now|buy now", -5),
    # --- added: pages and files that look close to policies but are not
    (r"claims? (?:forms?|submission|status|inquiry|reconsideration|dispute)|(?:enrollment|application|change|update|demographic|roster) forms?|"
     r"\bw-?9\b|direct deposit|electronic funds|\beft\b|\bera\b enrollment|credentialing|recredentialing|become a (?:participating )?provider|"
     r"join our network|provider enrollment|contracting", -5),
    (r"benefit (?:booklet|guide)|certificate of coverage|schedule of benefits|plan documents?|rate sheets?|premium|id cards?", -5),
    (r"member (?:rights|resources|services|portal|materials|education)|patient (?:education|resources)|health (?:tips|library|topics)|"
     r"wellness|healthy living|disease management|care management programs?", -4),
    (r"fraud|waste (?:and|&) abuse|compliance (?:plan|program)|code of conduct|\bhedis\b|\bncqa\b|quality (?:improvement|program)|star ratings?", -4),
    (r"\bappeals?\b|grievances?|complaints?", -2),
    (r"mail[\s-]order|home delivery|refills?|copay (?:card|savings)|coupons?|discount cards?|manufacturer (?:coupon|assistance)|"
     r"drug (?:recalls?|shortages?|safety alerts?)", -4),
    (r"subscribe|unsubscribe|email sign|mobile app|download (?:the )?app", -6),
]
# Excluded document types (provider manuals, bulletins). These lower the score of a DOCUMENT link only: a page about them can
# still be a hub that lists real policies ("Policy updates" pages), so page-following scores ignore them. The exclusion itself
# is done by excluded_doc_type() on the link text and again on the downloaded content.
DOC_ONLY_NEGATIVE_TERMS: list[tuple[str, float]] = [
    (r"provider (?:manuals?|handbooks?|administrative (?:guides?|manuals?)|reference (?:guides?|manuals?))|administrative guide|office manual", -4),
    (r"(?<!clinical policy )\bbulletins?\b|network news|provider news|polic(?:y|ies) (?:updates?|notifications?)|"
     r"notices? of (?:policy )?changes?|update notifications?", -4),
]
FIELD_WEIGHTS = {"anchor": 1.0, "url": 0.8, "heading": 0.6, "parent": 0.35, "page": 0.25}

_POS = [(re.compile(p, re.I), w) for p, w in POSITIVE_TERMS]
_NAV = [(re.compile(p, re.I), w) for p, w in NAV_TERMS]
_NEG = [(re.compile(p, re.I), w) for p, w in NEGATIVE_TERMS]
_NEG_DOC = [(re.compile(p, re.I), w) for p, w in DOC_ONLY_NEGATIVE_TERMS]

GENERIC_ANCHORS = re.compile(
    r"^(?:pdf|download|download pdf|click here|here|view|view pdf|view document|open|read more|learn more|"
    r"more|link|english|spanish|espa[nñ]ol|details|document|file|go|see more|\(pdf\)|\d+ ?kb|\d+(?:\.\d+)? ?mb)$", re.I)


def _match_terms(text: str, terms: list[tuple[re.Pattern, float]]) -> tuple[float, list[str]]:
    total, hits = 0.0, []
    for rx, w in terms:
        m = rx.search(text)
        if m:
            total += w
            hits.append(m.group(0).lower())
    return total, hits


_TERMSET_NAMES = {"pos": "_POS", "nav": "_NAV", "neg": "_NEG", "negdoc": "_NEG_DOC"}


@lru_cache(maxsize=200000)
def _match_cached(text: str, name: str) -> tuple[float, tuple]:
    total, hits = _match_terms(text, globals()[_TERMSET_NAMES[name]])
    return total, tuple(hits)


def _mt(text: str, name: str) -> tuple[float, list[str]]:
    """Same result as _match_terms(text, <lexicon>); repeated short texts are answered from a cache."""
    if len(text) <= 600:
        total, hits = _match_cached(text, name)
        return total, list(hits)
    return _match_terms(text, globals()[_TERMSET_NAMES[name]])


def score_link(url: str, anchor: str = "", heading: str = "", parent: str = "", page: str = "",
               is_page: bool = False, from_sitemap: bool = False) -> tuple[float, list[str]]:
    """Gate 1: weighted keyword score from URL, link text and surrounding context."""
    fields = {"anchor": anchor, "url": url_words(url) if url else "", "heading": heading, "parent": parent, "page": page}
    score, signals = 0.0, []
    for name, text in fields.items():
        if not text:
            continue
        text = text.lower()
        pos, ph = _mt(text, "pos")
        neg, nh = _mt(text, "neg")
        if not is_page:      # see DOC_ONLY_NEGATIVE_TERMS
            nd, ndh = _mt(text, "negdoc")
            neg, nh = neg + nd, nh + ndh
        if neg < 0 and pos <= 2:   # "policy" alone is a trap ("privacy policy")
            pos = 0
        total = pos + neg
        if is_page:
            nav, nvh = _mt(text, "nav")
            total += nav
            ph += nvh
        if name in ("parent", "page"):   # weak context fields can't carry a link on their own
            total = min(total, 3.0) if total > 0 else total
        score += total * FIELD_WEIGHTS[name]
        signals += [f"{name}:{h}" for h in ph + nh]
        
    # FIX 1: Allow extension bonus even if keyword score is 0 (but not negative)
    # Previously, score > 0 prevented generic URLs like /docs/12345.pdf from getting the bonus.
    if url and url_ext(url) in DOC_EXTS and score >= 0:
        score += 1.5
        signals.append(f"ext:{url_ext(url)}")
        
    # FIX 2: Document Repository Path Bonus
    # URLs in known document repositories are highly likely to be valid policies, 
    # even if the filename is generic. This filters out noisy links in /images/ or /assets/.
    low_url = url.lower()
    if re.search(r"/(?:documents?|polic(?:y|ies)|media|files?|dam|forms?|guidelines?|docs)/", low_url):
        score += 2.0
        signals.append("repo_path")

    # FIX 3: Sitemap Discovery Bonus
    # Sitemaps are curated by the site owner. A document found here is explicitly 
    # declared as important indexable content, reducing the noise risk.
    if from_sitemap and url_ext(url) in DOC_EXTS:
        score += 1.5
        signals.append("sitemap_doc")
        
    return round(score, 2), signals[:12]

_DECLARED_RX = [
    re.compile(r"\d[\d,]*\s*(?:-|–|to)\s*\d[\d,]*\s+of\s+([\d,]{2,9})\b", re.I),                       # 1-20 of 523
    re.compile(r"\b(?:showing|displaying|viewing)\s+\d[\d,]*\s+of\s+([\d,]{2,9})\b", re.I),             # showing 20 of 523
    re.compile(r"\b(?:total|of)\s*:?\s*([\d,]{2,9})\s+(?:results?|items?|documents?|policies|policy|guidelines?|forms?|"
               r"records?|entries|matches|files?)\b", re.I),                                           # of 523 policies
    re.compile(r"\b([\d,]{2,9})\s+(?:results?|documents?|policies|guidelines?|forms?|records?|matches)\s+"
               r"(?:found|available|total|match)", re.I),                                              # 523 results found
    re.compile(r"\bfound\s+([\d,]{2,9})\s+(?:results?|documents?|policies|guidelines?|forms?|records?|matches)\b", re.I),
]


def declared_count(text: str) -> tuple[int, str]:
    """The number of entries a listing page says it holds ('Showing 1-20 of 523', '523 policies found'), else (0, '').
    The largest plausible figure wins; bare years and tiny numbers are ignored."""
    head = (text or "")[:60000]
    best, snip = 0, ""
    for rx in _DECLARED_RX:
        for m in rx.finditer(head):
            raw = m.group(1)
            n = int(raw.replace(",", ""))
            if n < 10 or n > 500000 or ("," not in raw and 1990 <= n <= 2035):
                continue
            if n > best:
                best, snip = n, clean_text(m.group(0), 80)
    return best, snip


@lru_cache(maxsize=262144)
def link_kind(url: str) -> str:
    ext = url_ext(url)
    low = url.lower()
    if ext in JUNK_EXTS:
        return "junk"
    if ext in DOC_EXTS:
        return "doc"
    if ext == "xml":
        return "sitemap" if "sitemap" in low else "junk"
    if ext in PAGE_EXTS:
        return "page"
    if any(h in low for h in MAYBE_DOC_HINTS):
        return "maybe_doc"
    return "page"


# ---- document type: weighted vote (title/link > header (position-weighted) > listing page > body) -------------
DOC_TYPE_ORDER = ["formulary", "drug_list", "pa_form", "pa_list", "pharmacy_policy", "coverage_guideline",
                  "medical_policy", "provider_manual", "bulletin", "reimbursement"]
DOC_TYPE_PATTERNS: dict[str, list[tuple[str, float]]] = {
    "pa_form": [(r"(?:prior auth\w*|pre-?auth\w*|precert\w*|pre-?certification|pre-?determination|exception|"
                 r"coverage determination|redetermination|medication|drug|pharmacy|authorization|referral)\s+"
                 r"(?:request\s+)?forms?\b", 4), (r"\brequest forms?\b", 4), (r"\bfax (?:form|back)\b", 3)],
    "pa_list": [(r"(?:prior auth\w*|precert\w*|pre-?certification|pre-?authorization|pre-?service)\s+(?:code\s+)?"
                 r"(?:lists?|requirements?)", 4),
                (r"(?:codes?|services|procedures|drugs|medications) (?:that )?requir\w+ (?:prior auth\w*|precert\w*|"
                 r"pre-?authorization|pre-?certification)", 4),
                (r"prior authorization (?:and|&) notification", 3)],
    "formulary": [(r"formular(?:y|ies)", 4)],
    "drug_list": [(r"drug lists?|medication lists?|covered drugs? list|\bpdl\b|preferred drug|specialty (?:drug|pharmacy) list", 4)],
    "pharmacy_policy": [(r"(?:drug|pharmacy|medication|medical benefit drug|specialty drug|specialty pharmacy|injectable|"
                         r"biologic|rx) (?:clinical )?polic(?:y|ies)", 4), (r"clinical pharmacy", 3),
                        (r"drug (?:coverage )?(?:criteria|guidelines?)", 3), (r"step therapy|quantity limits?", 2)],
    "coverage_guideline": [(r"coverage determination guidelines?|clinical (?:utilization management |um )?guidelines?|"
                            r"medical necessity (?:guidelines?|criteria)|utilization management guidelines?|"
                            r"coverage guidelines?|clinical (?:review )?criteria|coverage criteria", 4)],
    "medical_policy": [(r"medical (?:coverage )?polic(?:y|ies)|coverage polic(?:y|ies)|clinical (?:coverage )?polic(?:y|ies)|"
                        r"clinical policy bulletins?|medical clinical policy|coverage rationale", 4)],
    "provider_manual": [(r"provider (?:manual|handbook|administrative (?:guide|manual)|reference (?:guide|manual))|"
                         r"administrative guide|office manual", 4)],
    "bulletin": [(r"\bbulletins?\b|polic(?:y|ies) updates?|notice of (?:policy )?changes?|policy notifications?|"
                  r"network news|update notification", 3)],
    "reimbursement": [(r"(?:reimbursement|payment) polic(?:y|ies)", 4)],
}
_DT_RX = {t: [(re.compile(p, re.I), w) for p, w in pats] for t, pats in DOC_TYPE_PATTERNS.items()}
FORM_FIELDS = re.compile(r"member (?:id|name|number)|subscriber id|\bnpi\b|prescriber|date of birth|\bdob\b|diagnosis|"
                         r"icd-?10 code|signature|fax (?:number|#|to)|phone (?:number|#)|requesting provider|ordering provider", re.I)


def classify_doc_type(own: str, header: str = "", body: str = "", around: str = "",
                      policy_number: str = "") -> tuple[str, str, bool]:
    """Weighted vote over evidence tiers. Returns (document_type, evidence_tier, is_reimbursement_policy).

    own    = title + link text + URL words (weight 3)
    header = first ~400 chars of the document (weight 2, x1.5 when the match is in the first 150 chars)
    around = heading / title of the page that listed the link (weight 1)
    body   = chars 400-3000 (weight 0.5)
    A medical policy that merely mentions "prior authorization requirements" deep in its header no longer flips to pa_list.
    """
    scores: dict[str, float] = defaultdict(float)
    src: dict[str, str] = {}
    tiers = (("title/link", own, 3.0), ("document header", header, 2.0), ("listing page", around, 1.0),
             ("document body", body, 0.5))
    for name, text, weight in tiers:
        if not text:
            continue
        for t, rxs in _DT_RX.items():
            best, pos = 0.0, 10 ** 6
            for rx, w in rxs:
                m = rx.search(text)
                if m and w >= best:
                    best, pos = w, min(pos, m.start())
            if best:
                mult = 1.5 if (name == "document header" and pos < 150) else 1.0
                scores[t] += best * weight * mult
                src.setdefault(t, name)
    head_body = (header + " " + body)[:2500]
    if head_body.strip():
        cues = {m.group(0).lower() for m in FORM_FIELDS.finditer(head_body)}
        if len(cues) >= 4 and re.search(r"\bform\b|\brequest\b", own + " " + header, re.I):
            scores["pa_form"] += 8
            src.setdefault("pa_form", "form fields")
    if policy_number and re.search(r"PHAR|(?:^|[.\-_])RX(?:$|[.\-_])|DRUG", policy_number, re.I):
        scores["pharmacy_policy"] += 14      # the publisher's own numbering scheme is decisive
        src.setdefault("pharmacy_policy", "policy number")
    if not scores:
        return "other", "none", False
    best_t = max(DOC_TYPE_ORDER, key=lambda t: (scores.get(t, 0.0), -DOC_TYPE_ORDER.index(t)))
    if scores.get(best_t, 0.0) < 3:
        return "other", "weak", False
    if best_t == "reimbursement":
        return "other", src[best_t], True
    return best_t, src[best_t], False


def excluded_doc_type(own: str, header: str = "", body: str = "", around: str = "", policy_number: str = "") -> str:
    """The document type from EXCLUDED_DOC_TYPES that classify_doc_type picks for this evidence, else ''."""
    dtype = classify_doc_type(own, header, body, around, policy_number)[0]
    return dtype if dtype in EXCLUDED_DOC_TYPES else ""


# ---- line of business --------------------------------------------------------
LOB_RULES: dict[str, list[str]] = {
    "Federal": [r"federal employee program", r"\bfep\b", r"\bfehbp?\b", r"\btricare\b", r"federal employees? health benefits?"],
    "Medicare": [r"medicare advantage", r"\bmedicare\b(?!\s+(?:coverage database|national coverage|local coverage|"
                 r"coverage determination|\(?ncd|\(?lcd|benefit policy manual|claims processing|program integrity))",
                 r"\bpart d\b", r"\bmapd\b", r"\bpdp\b", r"\bd-?snp\b", r"dual (?:eligible|special needs|complete)", r"\bmedigap\b"],
    "Medicaid": [r"\bmedicaid\b", r"\bchip\b", r"community plan", r"familycare", r"healthchoices", r"tenncare", r"medi-cal",
                 r"star ?(?:\+|plus|kids)\b", r"badgercare", r"masshealth", r"healthy michigan", r"medical assistance",
                 r"\bmltss\b", r"\bltss\b", r"managed long[\s-]term"],
    "Exchange": [r"marketplace", r"health insurance exchange", r"\bexchange plans?\b", r"\bon[\s-]exchange\b",
                 r"individual (?:and|&) family (?:plans?|products?)", r"\bifp\b", r"affordable care act", r"\baca\b", r"\bambetter\b"],
    "Commercial": [r"\bcommercial\b", r"\bemployer\b", r"group (?:plans?|health)", r"fully[\s-]insured", r"self[\s-]funded",
                   r"large group", r"small group", r"employer[\s-]sponsored"],
}
_LOB_RX = {k: [re.compile(p, re.I) for p in v] for k, v in LOB_RULES.items()}
LOB_HEADER_CHARS = 300   # body text is only consulted in the document header ("plasma exchange" deeper in a policy is not a LOB)


# unambiguous cues only: used on the opening of the document when nothing else names a line of business
_LOB_BODY_RX = {k: [re.compile(p, re.I) for p in v] for k, v in {
    "Medicare": [r"medicare advantage", r"\bpart d\b", r"\bmapd\b"],
    "Medicaid": [r"\bmedicaid\b", r"community plan"],
    "Federal": [r"federal employee program", r"\bfep\b"],
    "Exchange": [r"\bmarketplace\b", r"\bon[\s-]exchange\b"],
    "Commercial": [r"\bcommercial (?:plans?|members?|products?|policies)\b"],
}.items()}
LOB_BODY_CHARS = 1500


def detect_lob(own: str, header: str, around: str, body: str = "") -> tuple[str, str]:
    for name, text in (("title/link", own), ("document header", (header or "")[:LOB_HEADER_CHARS]), ("listing page", around)):
        if not text:
            continue
        found = [k for k, rxs in _LOB_RX.items() if any(rx.search(text) for rx in rxs)]
        if len(found) == 1:
            return found[0], name
        if len(found) > 1:
            return "Multiple", name
    found = [k for k, rxs in _LOB_BODY_RX.items() if any(rx.search((body or "")[:LOB_BODY_CHARS]) for rx in rxs)]
    if len(found) == 1:
        return found[0], "document body"
    return "Unknown", "none"


# ---- state ---------------------------------------------------------------------
STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR",
}
USPS_CODES = set(STATE_NAMES.values())
# codes that collide with common healthcare abbreviations (PA=prior auth, MA=Medicare Advantage, CT scan, MS ...)
_AMBIGUOUS_CODES = {"pa", "ma", "ct", "ms", "md", "va", "de", "id", "me", "or", "hi", "ok", "co", "al", "la", "il",
                    "ne", "mo", "in", "oh", "wa", "ga", "mt", "sc", "nd", "ri"}
_STATE_NAME_RX = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in STATE_NAMES), key=len, reverse=True)) + r")\b")
# state names that do not mean "this document is scoped to that state"
_STATE_FALSE_POS = re.compile(
    r"\b(?:New York Heart Association|NYHA|Washington Manual|George Washington University|Virginia Mason|"
    r"University of (?:[A-Z][a-z]+ ){0,2}[A-Z][a-z]+|"
    r"(?:[A-Z][a-z]+ ){1,2}(?:State )?(?:University|College|Medical Center|Hospital|Heart Association|Academy|Society|"
    r"Medical Association|Cancer Center|Clinic|School of Medicine)s?)\b")


def _states_in_text(text: str) -> list[str]:
    text = _STATE_FALSE_POS.sub(" ", text or "")
    out: list[str] = []
    for m in _STATE_NAME_RX.finditer(text):
        code = STATE_NAMES[m.group(1)]
        if code == "WA" and re.match(r"\s*,?\s*D\.?\s?C\.?", text[m.end():m.end() + 7]):
            code = "DC"
        if code not in out:
            out.append(code)
    return out


def _states_in_url(url: str) -> list[str]:
    p = urlparse(url)
    out: list[str] = []
    for seg in re.split(r"[/_\-.]", unquote(p.path).lower()):
        if len(seg) == 2 and seg.upper() in USPS_CODES and seg not in _AMBIGUOUS_CODES:
            out.append(seg.upper())
    for k, v in parse_qsl(p.query):
        if k.lower() in ("state", "st", "region") and v.upper() in USPS_CODES:
            out.append(v.upper())
    out += _states_in_text(url_words(url).title())
    return list(dict.fromkeys(out))


def detect_state(url: str, own: str, header: str, around: str, footprint: str) -> tuple[str, str]:
    """Two-letter USPS codes (pipe-joined) or National. Evidence order: URL + title/link text, document header,
    listing page heading, then (optionally) the payer's single-state footprint. Never guessed otherwise."""
    s = list(dict.fromkeys(_states_in_url(url) + _states_in_text(own)))
    if s:
        return STATE_JOINER.join(s), "title/link/url"
    s = _states_in_text((header or "")[:400])
    if s:
        return STATE_JOINER.join(s), "document header"
    s = _states_in_text(around)
    if s:
        return STATE_JOINER.join(s), "listing page"
    if footprint and INFER_STATE_FROM_PAYER_FOOTPRINT:
        return footprint, "payer footprint"
    return STATE_NATIONAL, "default"


# ---- content fingerprint (Gate 2) ------------------------------------------------
CONTENT_POS: list[tuple[str, float]] = [
    (r"policy (?:number|no\.?|#|id)", 3), (r"effective date|effective:", 2), (r"medically necessary|medical necessity", 3),
    (r"coverage (?:criteria|rationale|determination)|criteria for (?:coverage|approval)|clinical criteria", 3),
    (r"\bcpt\b|\bhcpcs\b", 2), (r"\bicd-?10\b", 1), (r"prior (?:authorization|approval)|precertification|pre-?certification", 2),
    (r"formulary|drug list|preferred drug", 3), (r"\btier [1-6]\b|\btier\s+(?:one|two|three)", 2),
    (r"quantity limit|step therapy", 2), (r"experimental (?:or|/|and) investigational|investigational", 2),
    (r"\bapplicable codes\b|\bcoding information\b|\bbilling/coding\b", 2), (r"\breferences\b", 0.5),
    (r"\b(?:background|description|rationale|scope)\b", 0.5), (r"last review(?:ed)?|review date|revision history|policy history", 1.5),
    (r"reimbursement|payment policy", 1.5), (r"\bguideline\b", 1),
]
CONTENT_NEG: list[tuple[str, float]] = [
    (r"notice of privacy practices|privacy notice|privacy policy", -4), (r"summary of benefits and coverage", -4),
    (r"evidence of coverage", -4), (r"annual notice of changes", -4), (r"member handbook", -4),
    (r"press release|for immediate release", -4), (r"newsletter", -2), (r"job description|apply now|equal opportunity employer", -4),
    (r"terms of use|terms and conditions", -3), (r"annual report|form 10-k", -4), (r"nondiscrimination notice|language assistance services", -3),
]
_CPOS = [(re.compile(p, re.I), w) for p, w in CONTENT_POS]
_CNEG = [(re.compile(p, re.I), w) for p, w in CONTENT_NEG]
_CPT_RX = re.compile(r"\b(?:[0-9]{4}[0-9FTU]|[A-V][0-9]{4})\b")


_STRONG_DOC_MARKERS = re.compile(r"policy (?:number|no\.?|#)|effective date|medically necessary|medical necessity|"
                                 r"coverage (?:criteria|rationale)|clinical criteria|\bcpt\b|\bhcpcs\b|\btier [1-6]\b", re.I)


def html_policy_evidence(ins: "Inspection") -> tuple[bool, float, list[str]]:
    """An HTML page is a policy document only on its own (non-link) text: not an index/hub, a content score >= 5 and at
    least two distinct document markers (policy number, effective date, medical necessity, CPT/HCPCS, tiers ...)."""
    text = ins.nonlink_text or ""
    cs, sig = content_fingerprint(text)
    # many document links alone do not make an index (a policy page can carry a Related Documents / History sidebar):
    # it is an index only when the prose left after removing link text is thin
    is_index = ins.link_density > 0.35 or len(text) < 600 or (ins.doc_links >= 8 and len(text) < 2000)
    strong = len({m.group(0).lower() for m in _STRONG_DOC_MARKERS.finditer(text[:8000])})
    return (not is_index and cs >= 5 and strong >= 2), cs, sig


def content_fingerprint(text: str) -> tuple[float, list[str]]:
    head = (text or "")[:8000]
    if not head.strip():
        return 0.0, []
    pos, ph = _match_terms(head, _CPOS)
    neg, nh = _match_terms(head, _CNEG)
    codes = len(set(_CPT_RX.findall(head)))
    bonus = 2.0 if codes >= 5 else (1.0 if codes >= 2 else 0.0)
    return round(pos + neg + bonus, 2), (ph + nh + ([f"codes:{codes}"] if bonus else []))[:14]


# =============================================================================
# 7. METADATA EXTRACTION (regex first; values as printed, never inferred)
# =============================================================================
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PAT = (rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}|{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|"
            rf"\d{{1,2}}\s+{_MONTH}\.?,?\s+\d{{4}}|{_MONTH}\.?,?\s+\d{{4}}|\d{{1,2}}/\d{{4}})")
_EFFECTIVE_RX = re.compile(rf"((?:original|initial)\s+)?effective(?:\s+date)?(?:\s+of\s+(?:policy|this policy))?"
                           rf"(?:\s+(?:for\s+(?:dates\s+of\s+service|claims?|services?)\s+)?(?:on\s+or\s+after|on|as\s+of|beginning|starting|from))?"
                           rf"\s*[:\-–]?\s*({DATE_PAT})", re.I)
_UPDATED_RX = re.compile(
    rf"(?:last\s+(?:reviewed|review(?:\s+date)?|revised|revision(?:\s+date)?|updated|modified|approved)|review(?:ed)?\s+date|"
    rf"revision\s+date|revised(?:\s+date)?|updated(?:\s+on)?|date\s+(?:of\s+)?(?:last\s+)?(?:review|revision|update)|"
    rf"approval\s+date|approved(?:\s+date)?|date\s+issued|issue\s+date|(?:last\s+)?(?:reviewed|review)\s*/\s*(?:revised|revision)(?:\s+date)?|"
    rf"(?:revised|revision)\s*/\s*(?:reviewed|review)(?:\s+date)?|policy\s+(?:last\s+)?(?:updated|revised))\s*(?:on)?\s*[:\-–]?\s*({DATE_PAT})", re.I)
_NOT_PAST = re.compile(r"(?:next|scheduled|upcoming|due|planned|future)\s*$", re.I)


@lru_cache(maxsize=65536)
def parse_date(s: str) -> tuple[str, str]:
    """-> (YYYY-MM-DD or '', kind) with kind 'full' | 'month_year' | ''. A month/year without a day is NOT
    turned into a date (the schema says: as printed, empty rather than inferred)."""
    s = clean_text(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s or "", flags=re.I))
    if not s:
        return "", ""
    if re.fullmatch(rf"{_MONTH}\.?,?\s+\d{{4}}|\d{{1,2}}/\d{{4}}", s, re.I):
        return "", "month_year"
    try:
        d = dateparser.parse(s, dayfirst=False, default=datetime(2000, 1, 1))
    except (ValueError, OverflowError, TypeError):
        return "", ""
    if not d or not (1990 <= d.year <= datetime.now().year + 2):
        return "", ""
    return d.strftime("%Y-%m-%d"), "full"


def http_date_to_iso(value: str) -> str:
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return ""
    if d is None or not (1990 <= d.year <= datetime.now().year + 1):
        return ""
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d") if d.tzinfo else d.strftime("%Y-%m-%d")


def _scan_dates(head: str) -> dict[str, Any]:
    """Labelled dates only. effective_date: the current 'Effective Date' (an 'Original Effective Date' is used only when
    nothing else is printed, and noted). last_updated_date: latest 'last reviewed / revised / updated' date that is not
    in the future and not labelled 'next review'."""
    out: dict[str, Any] = {"effective_date": "", "last_updated_date": "", "notes": []}
    current, original, partial = [], [], []
    for m in _EFFECTIVE_RX.finditer(head):
        d, kind = parse_date(m.group(2))
        if kind == "month_year":
            partial.append(clean_text(m.group(2)))
            if not current and not original:
                break          # the first labelled effective date is month/year only: leave empty (as printed)
            continue
        if d:
            (original if m.group(1) else current).append(d)
            if current:
                break
    if current:
        out["effective_date"] = current[0]
    elif original:
        out["effective_date"] = original[0]
        out["notes"].append("effective_date is the printed 'original effective date' (no current one printed)")
    elif partial:
        out["notes"].append(f"effective date printed as '{partial[0]}' without a day; left empty")
    today = date.today().isoformat()
    updated, upd_partial = [], []
    for m in _UPDATED_RX.finditer(head):
        if _NOT_PAST.search(head[max(0, m.start() - 14):m.start()]):
            continue
        d, kind = parse_date(m.group(1))
        if kind == "month_year":
            upd_partial.append(clean_text(m.group(1)))
        elif d and d <= today:
            updated.append(d)
    if updated:
        out["last_updated_date"] = max(updated)
    elif upd_partial:
        out["notes"].append(f"last review/revision date printed as '{upd_partial[0]}' without a day; left empty")
    return out


def extract_dates(text: str, tail: str = "") -> dict[str, Any]:
    """_scan_dates over the start of the document; a date still missing is looked for at the end (footer / revision
    history / last pages) and noted as such."""
    out = _scan_dates((text or "")[:10000])
    if tail and not (out["effective_date"] and out["last_updated_date"]):
        t = _scan_dates(tail[:8000])
        if not out["effective_date"] and t["effective_date"]:
            out["effective_date"] = t["effective_date"]
            out["notes"] = [n for n in out["notes"] if not n.startswith("effective date printed")]
            out["notes"] += [n for n in t["notes"] if n.startswith("effective_date is")]
            out["notes"].append("effective_date taken from the end of the document")
        if not out["last_updated_date"] and t["last_updated_date"]:
            out["last_updated_date"] = t["last_updated_date"]
            out["notes"] = [n for n in out["notes"] if not n.startswith("last review/revision date printed")]
            out["notes"].append("last_updated_date taken from the end of the document")
    return out


_POLICY_LABEL_RX = re.compile(
    r"(?:medical\s+|clinical\s+|coverage\s+|payment\s+|reimbursement\s+|drug\s+|pharmacy\s+)?"
    r"(?:policy|guideline|bulletin|document|cpb|reference)\s*(?:number|no\.?|num\.?|#|id|code)\s*[:#\-–]?\s*"
    r"([A-Z0-9][A-Z0-9.\-_/]{1,29})", re.I)
# publisher formats - searched in the title and the first 600 characters only
_POLICY_FORMAT_RX = [
    re.compile(r"\b20\d{2}[A-Z]\d{4}[A-Z0-9]{0,3}\b"),                                         # UHC   2025T0547R
    re.compile(r"\b(?:CG|MED|SURG|DME|RAD|LAB|GENE|BEH|ADMIN|DRUG|ANC|TRANS|THER-RX|OR-PR|INT|PSYCH|REHAB)-"
               r"(?:[A-Z]{2,5}-)?\d{2,5}\b"),                                                 # Anthem CG-MED-12, SURG.00011
    re.compile(r"\b(?:MED|SURG|DME|RAD|LAB|GENE|BEH|ADMIN|DRUG|ANC|TRANS|INT|PSYCH|REHAB)\.\d{5}\b"),  # Anthem MED.00002
    re.compile(r"\b(?:CP|HIM|PA)\.[A-Z]{2,6}\.\d{2,5}[A-Z]?\b"),                               # Centene CP.MP.107 / CP.PHAR.123
]
_GENERIC_ID_RX = re.compile(r"\b[A-Z]{1,4}-\d{2,5}(?:-\d{1,3})?\b")   # title only
_NOT_POLICY_IDS = re.compile(r"^(?:ICD|CPT|HCPCS|COVID|SARS|HIV|HPV|IL|CD|HER|BRCA|TNF|FDA|CFR|ISO|ANSI|HL|NCD|LCD|PD|"
                             r"CYP|MTHFR|EGFR|ALK|KRAS|BCR|PCR|NYHA|ECOG|GLP|SGLT|PCSK|DPP)\b", re.I)


def _valid_policy_number(v: str) -> bool:
    v = v.strip(".-_/:")
    if len(v) < 2 or len(v) > 30 or not re.search(r"\d", v):
        return False
    if _NOT_POLICY_IDS.match(v):
        return False
    if re.fullmatch(DATE_PAT, v, re.I) or re.fullmatch(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", v):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", v):   # a bare year
        return False
    return True


def extract_policy_number(text: str, title: str, tail: str = "", url: str = "") -> tuple[str, str]:
    """Publisher's own identifier, verbatim (leading zeros kept). Order: labelled in the first pages, a publisher format in
    the title / first 600 chars, labelled at the end of the document, a generic id in the title, and last a publisher
    format in the file name (source 'url', the caller notes it)."""
    head = (text or "")[:6000]
    for m in _POLICY_LABEL_RX.finditer(head):
        v = m.group(1).strip(".-_/:")
        if _valid_policy_number(v):
            return v, "labeled"
    for src in (title or "", head[:600]):
        for rx in _POLICY_FORMAT_RX:
            m = rx.search(src)
            if m and _valid_policy_number(m.group(0)):
                return m.group(0), "format"
    for m in _POLICY_LABEL_RX.finditer((tail or "")[:6000]):
        v = m.group(1).strip(".-_/:")
        if _valid_policy_number(v):
            return v, "labeled"
    m = _GENERIC_ID_RX.search(title or "")
    if m and _valid_policy_number(m.group(0)):
        return m.group(0), "format"
    if url:
        path = re.sub(r"[_/]", " ", unquote(urlparse(url).path))
        for rx in _POLICY_FORMAT_RX:
            m = rx.search(path)
            if m and _valid_policy_number(m.group(0)):
                return m.group(0), "url"
    return "", ""


GENERIC_TITLES = re.compile(
    r"^(?:medical|clinical|coverage|reimbursement|payment|drug|pharmacy|commercial|medicare|medicaid)?\s*"
    r"(?:medical\s+)?(?:policy|policies|guideline|guidelines|bulletin|criteria|form|document|formulary|clinical policy)?\s*:?\s*$", re.I)
_BRANDS = sorted({b.lower() for p in PAYER_REGISTRY for b in [p["payer_name"], p.get("payer_alias", "")] + p.get("brand_terms", []) if b}
                 | {"unitedhealthcare", "united healthcare", "uhc", "anthem", "elevance health", "cigna", "horizon", "upmc",
                    "caresource", "centene", "geisinger", "highmark", "bluecross blueshield", "blue cross blue shield",
                    "blue cross", "blue shield", "bcbs", "health plan", "healthcare", "inc."}, key=len, reverse=True)
_BRAND_RX = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(b) for b in _BRANDS) + r")(?!\w)", re.I)


_BRAND_LOCK = threading.Lock()


def register_brands(terms: list[str]) -> None:
    """Add the current payer's names to the boilerplate brand list used by title trimming."""
    global _BRANDS, _BRAND_RX
    with _BRAND_LOCK:
        new = {t.lower().strip() for t in terms if t and len(t.strip()) >= 3} - set(_BRANDS)
        if new:
            _BRANDS = sorted(set(_BRANDS) | new, key=len, reverse=True)
            _BRAND_RX = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(b) for b in _BRANDS) + r")(?!\w)", re.I)

_SITE_BOILER = re.compile(r"^(?:home|homepage|providers?|for providers|provider (?:portal|center|resources|home)|"
                          r"health ?care professionals|for health ?care professionals|official site|members?|welcome|"
                          r"of [a-z ]+|and [a-z ]+)?$", re.I)


def is_good_title(t: str, payer_alias: str = "") -> bool:
    t = clean_text(t)
    if len(t) < 4 or len(t) > 300:
        return False
    low = t.lower()
    debranded = clean_text(_BRAND_RX.sub(" ", re.sub(r"[®™©*]", "", low)))
    if payer_alias:
        debranded = clean_text(debranded.replace(payer_alias.lower(), " "))
    if GENERIC_ANCHORS.match(low) or GENERIC_TITLES.match(debranded) or len(debranded) < 3 or _SITE_BOILER.match(debranded):
        return False
    if re.match(r"^(microsoft (word|excel|powerpoint)\s*-|untitled|document\d*$|slide \d|page \d)", low):
        return False
    if re.fullmatch(r"[\w\-. ]+\.(pdf|docx?|xlsx?)", low):
        return False
    if len(t.split()) > 30 or (t.endswith(".") and len(t.split()) > 14):   # a sentence, not a title
        return False
    return bool(re.search(r"[a-z]{3}", low))


def trim_title(t: str) -> str:
    """Remove site / brand boilerplate segments: 'Bariatric Surgery | UnitedHealthcare Provider' -> 'Bariatric Surgery'.
    Meaningful segments are kept: 'Multiple Sclerosis - Medical Clinical Policy Bulletin 0264' stays as is."""
    t = clean_text(t)
    parts = re.split(r"(\s+[|–—]\s+|\s+-\s+|\s+::\s+|\s+»\s+)", t)
    segs, seps = parts[0::2], parts[1::2]

    def boiler(s: str) -> bool:
        x = clean_text(_BRAND_RX.sub(" ", re.sub(r"[®™©]", "", s)))
        x = clean_text(re.sub(r"\b(?:inc|llc|corp(?:oration)?|company|co)\b\.?", " ", x, flags=re.I))
        return not x or bool(_SITE_BOILER.match(x))

    while len(segs) > 1 and boiler(segs[-1]):
        segs.pop()
        seps.pop()
    while len(segs) > 1 and boiler(segs[0]):
        segs.pop(0)
        seps.pop(0)
    out = segs[0]
    for sep, seg in zip(seps, segs[1:]):
        out += sep + seg
    return clean_text(out)


def title_from_filename(url: str) -> str:
    name = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"[_\-+]+", " ", name)
    return clean_text(name) or url


def clean_anchor(a: str) -> str:
    a = clean_text(a)
    a = re.sub(r"\(?\s*(?:pdf|docx?|xlsx?)\s*(?:,\s*)?(?:\d+(?:\.\d+)?\s*(?:kb|mb))?\s*\)?\s*$", "", a, flags=re.I)
    a = re.sub(r"^\s*(?:download|view|open)\s*[:\-]?\s*", "", a, flags=re.I)
    return clean_text(a, 300)


def text_fingerprint(text: str) -> str:
    """Hash of the normalised extracted text - catches the same document re-saved / re-exported with different bytes."""
    t = (text or "").lower()
    t = re.sub(r"page\s+\d+\s+(?:of\s+\d+)?", " ", t)                                  # page footers
    t = re.sub(r"(?:printed|downloaded|generated|accessed|retrieved)(?: on)?[: ]+\S+(?: \d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?)?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return sha256_hex(t.encode("utf-8")) if len(t) >= TEXT_FP_MIN_CHARS else ""

