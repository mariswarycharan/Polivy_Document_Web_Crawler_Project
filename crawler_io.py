#!/usr/bin/env python3
"""Module 3/5 - I/O layer: content inspection (Gate 2 data), HTML link extraction, HTTP fetcher and the headless browser.

Part of the Payer Policy Document Discovery Crawler (v2). Logic is unchanged from the single-file version."""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

import hashlib
import httpx
import io
import json
import logging
import re
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from protego import Protego
from typing import Any, Optional
from urllib.parse import urlparse

try:  # selectolax >= 1.0 ships the Lexbor backend only
    from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node
except ImportError:  # older selectolax
    from selectolax.parser import HTMLParser, Node
try:
    import brotli  # only needed if a server ignores "Accept-Encoding: identity" and sends br
except ImportError:  # pragma: no cover
    brotli = None
try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

from crawler_config import (BYPASS_ROBOTS_FOR_DOCUMENT_URLS, BLOCK_TRACKER_REQUESTS_IN_BROWSER, FETCH_WORKERS, TRACKER_HOST_SUFFIXES, CONTACT, IDP_HOST_SUFFIXES, MAX_CRAWL_DELAY_SECONDS, MAX_EXPAND_CLICKS, MAX_PAGINATION_STEPS, MAX_PARSE_BYTES, MAX_RETRIES, MAX_SELECT_OPTIONS, MAX_SNAPSHOTS, MAX_STREAM_BYTES, MAX_TAB_CLICKS, PLAYWRIGHT_NAV_TIMEOUT_MS, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT_SECONDS, RETRY_STATUSES, TEXT_FP_MAX_PAGES, USER_AGENT, USE_OCR, USE_PLAYWRIGHT, log, utc_now_iso)
from crawler_text import (MAYBE_DOC_HINTS, is_cdn_host, DOC_EXTS, clean_text, clean_text_keep_lines, host_has_suffix, http_date_to_iso, is_good_title, link_kind, normalize_url, sha256_hex, trim_title, url_ext, url_key)


# =============================================================================
# 8. CONTENT INSPECTION (Gate 2 data source)
# =============================================================================
@dataclass
class Inspection:
    file_type: str = "other"
    text: str = ""                 # first pages / main text, used for classification + metadata
    fp_text: str = ""              # longer text used only for the near-duplicate fingerprint
    heading_title: str = ""        # PDF: largest heading on page 1
    meta_title: str = ""           # PDF / Office metadata title
    titles: list[str] = field(default_factory=list)   # HTML: h1 then <title> (trimmed)
    canonical_url: str = ""
    nonlink_text: str = ""         # HTML: main text without link text (a hub's link labels are not evidence)
    pages: int = 0
    readable: bool = False
    ocr: bool = False
    link_density: float = 0.0
    doc_links: int = 0
    notes: list[str] = field(default_factory=list)
    margin_text: str = ""         # headers/footers: first + last lines of every page (policy numbers / dates repeat there)


def sniff_file_type(data: bytes, content_type: str, url: str) -> str:
    """Magic bytes first, Content-Type second; the URL suffix only separates doc/xls inside an OLE container."""
    head = data[:2048]
    ct = (content_type or "").lower()
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
        except zipfile.BadZipFile:
            pass
        if "wordprocessingml" in ct:
            return "docx"
        if "spreadsheetml" in ct:
            return "xlsx"
        return "other"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        if "excel" in ct or "spreadsheet" in ct:
            return "xls"
        if "word" in ct:
            return "doc"
        return "xls" if url_ext(url) == "xls" else "doc"
    if "html" in ct or re.search(rb"<(?:!doctype html|html|head|body)\b", head, re.I):
        return "html"
    if "pdf" in ct:
        return "pdf"
    return "other"   # csv / txt / rtf / unknown are not schema file types


def inspect_pdf(data: bytes, payer_alias: str) -> Inspection:
    ins = Inspection(file_type="pdf")
    if fitz is None:
        ins.notes.append("PyMuPDF not installed")
        return ins
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # corrupt / truncated
        ins.notes.append(f"PDF could not be opened ({type(e).__name__})")
        return ins
    try:
        if doc.needs_pass:
            ins.notes.append("PDF is password protected")
            return ins
        ins.pages = doc.page_count
        meta = doc.metadata or {}

        def page_text(i: int) -> str:
            try:
                return doc[i].get_text("text")
            except Exception:
                return ""

        n_fp = min(doc.page_count, TEXT_FP_MAX_PAGES)
        texts = [page_text(i) for i in range(n_fp)]
        ins.text = "\n".join(texts[:3])
        tail = [page_text(i) for i in range(max(n_fp, doc.page_count - 2), doc.page_count)]
        ins.fp_text = "\n".join(texts + tail) + f"\n[pages {doc.page_count}]"
        ins.margin_text = "\n".join(t[:350] + "\n" + t[-350:] for t in texts + tail if t)[:40000]
        if doc.page_count:
            page = doc[0]
            spans = []
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                blocks = []
            for b in blocks:
                for line in b.get("lines", []):
                    for sp in line.get("spans", []):
                        t = clean_text(sp.get("text", ""))
                        if len(t) >= 2 and sp["bbox"][1] < page.rect.height * 0.6:
                            spans.append((round(sp.get("size", 0), 1), sp["bbox"][1], sp["bbox"][0], t))
            for size in sorted({s[0] for s in spans}, reverse=True)[:4]:
                t = clean_text(" ".join(s[3] for s in sorted((s for s in spans if s[0] == size), key=lambda s: (s[1], s[2]))), 250)
                if is_good_title(trim_title(t), payer_alias):
                    ins.heading_title = trim_title(t)
                    break
        mt = clean_text(meta.get("title", ""))
        if is_good_title(mt, payer_alias):
            ins.meta_title = mt
        if len(clean_text(ins.text)) >= 80:
            ins.readable = True
        elif USE_OCR and doc.page_count:
            ocr = _ocr_first_page(doc)
            if ocr:
                ins.text, ins.fp_text, ins.readable, ins.ocr = ocr, ocr, True, True
                ins.notes.append("scanned PDF read with OCR (page 1)")
            else:
                ins.notes.append("scanned PDF without a text layer; OCR unavailable")
    finally:
        doc.close()
    return ins


def _ocr_first_page(doc) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        pix = doc[0].get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img)[:8000]
    except Exception:
        return ""


def _zip_text(data: bytes, members: list[str], limit: int) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    out, size = [], 0
    for name in z.namelist():
        if any(name.startswith(m) for m in members):
            xml = z.read(name).decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>|</si>|</row>", "\n", xml)
            piece = re.sub(r"<[^>]+>", " ", xml)
            out.append(piece)
            size += len(piece)
        if size > limit * 2:
            break
    return clean_text_keep_lines(" ".join(out))[:limit]


def _zip_core_title(data: bytes) -> str:
    try:
        xml = zipfile.ZipFile(io.BytesIO(data)).read("docProps/core.xml").decode("utf-8", "ignore")
        m = re.search(r"<dc:title>(.*?)</dc:title>", xml, re.S)
        return clean_text(m.group(1)) if m else ""
    except Exception:
        return ""


def inspect_html(data: bytes, url: str, payer_alias: str) -> Inspection:
    ins = Inspection(file_type="html")
    html = data.decode("utf-8", "ignore") if isinstance(data, bytes) else data
    tree = HTMLParser(html)
    can = tree.css_first("link[rel=canonical]")
    if can is not None:
        ins.canonical_url = normalize_url(can.attributes.get("href") or "", url)
    title = clean_text(tree.css_first("title").text() if tree.css_first("title") else "")
    h1 = clean_text(tree.css_first("h1").text() if tree.css_first("h1") else "")
    for node in tree.css("script, style, noscript, nav, header, footer, svg, form, iframe"):
        node.decompose()
    body = tree.body
    text = clean_text_keep_lines(body.text(separator="\n", strip=True) if body else "")
    ins.text = text[:20000]
    ins.fp_text = text[:400000]
    link_chars = sum(len(clean_text(a.text())) for a in tree.css("a"))
    ins.link_density = round(link_chars / max(1, len(clean_text(text))), 3)
    ins.doc_links = sum(1 for a in tree.css("a[href]") if url_ext(normalize_url(a.attributes.get("href") or "", url)) in DOC_EXTS)
    for a in tree.css("a"):
        a.decompose()
    ins.nonlink_text = clean_text_keep_lines(body.text(separator="\n", strip=True) if body else "")[:20000]
    ins.margin_text = (text[:600] + "\n" + text[-2500:]) if len(text) > 3100 else ""
    for t in (h1, trim_title(title)):
        if t and is_good_title(t, payer_alias) and t not in ins.titles:
            ins.titles.append(t)
    ins.readable = len(ins.text) >= 200
    return ins


def inspect_content(data: bytes, content_type: str, url: str, payer_alias: str) -> Inspection:
    ft = sniff_file_type(data, content_type, url)
    if ft == "pdf":
        return inspect_pdf(data, payer_alias)
    if ft == "html":
        return inspect_html(data, url, payer_alias)
    ins = Inspection(file_type=ft)
    if ft == "docx":
        ins.fp_text = _zip_text(data, ["word/document.xml"], 400000)
        ins.text = ins.fp_text[:12000]
        t = _zip_core_title(data)
        if is_good_title(t, payer_alias):
            ins.meta_title = t
        first = next((l for l in ins.text.splitlines()[:8] if is_good_title(l, payer_alias)), "")
        if first:
            ins.heading_title = clean_text(first, 250)
    elif ft == "xlsx":
        ins.fp_text = _zip_text(data, ["xl/sharedStrings.xml", "xl/worksheets/"], 400000)
        ins.text = ins.fp_text[:12000]
        t = _zip_core_title(data)
        if is_good_title(t, payer_alias):
            ins.meta_title = t
    elif ft in ("doc", "xls"):
        ins.notes.append(f"legacy .{ft} file - text not parsed")
    else:
        sample = data[:20000].decode("utf-8", "ignore")
        if sample and re.search(r"[A-Za-z]{4}", sample) and "\x00" not in sample:
            ins.text = ins.fp_text = sample
        ins.notes.append("file type not in schema list (csv/txt/rtf/unknown) - typed other")
    ins.readable = len(clean_text(ins.text)) >= 80
    return ins


# =============================================================================
# 9. HTML LINK EXTRACTION (with context for Gate 1)
# =============================================================================
@dataclass
class Link:
    url: str
    anchor: str = ""
    heading: str = ""
    parent: str = ""
    origin: str = "a"            # a | option | iframe | onclick | script | xhr
    hints: dict = field(default_factory=dict)
    label: str = ""              # browser interaction that revealed the link ('' = visible on load)


VIA_BY_ORIGIN = {"a": "css_selector", "option": "css_selector", "iframe": "css_selector", "onclick": "css_selector",
                 "script": "regex", "xhr": "json_api"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_URL_IN_TEXT = re.compile(r"""["'(]((?:https?:)?/[^"'()\s<>]+?\.(?:pdf|docx?|xlsx?|csv)(?:\?[^"'()\s<>]*)?)["')]""", re.I)
_ONCLICK_URL = re.compile(r"""(?:window\.open|location(?:\.href)?\s*=|open\w*\()\s*\(?\s*['"]([^'"]+)['"]""", re.I)


def _nearest_heading(node: Node, max_up: int = 6, max_sibs: int = 25) -> str:
    cur = node
    for _ in range(max_up):
        sib, n = cur.prev, 0
        while sib is not None and n < max_sibs:
            tag = sib.tag
            if tag in _HEADING_TAGS:
                return clean_text(sib.text(), 150)
            if tag not in ("-text", "-comment", "script", "style"):
                hs = sib.css("h1, h2, h3, h4, h5, h6")
                if hs:
                    return clean_text(hs[-1].text(), 150)
                if tag in ("strong", "b", "dt", "legend", "caption") or "heading" in (sib.attributes.get("class") or "").lower():
                    t = clean_text(sib.text(), 150)
                    if 3 <= len(t) <= 120:
                        return t
            sib, n = sib.prev, n + 1
        cur = cur.parent
        if cur is None or cur.tag in ("body", "html"):
            break
    return ""


def _parent_context(node: Node) -> str:
    cur = node.parent
    for _ in range(4):
        if cur is None:
            return ""
        if cur.tag in ("li", "tr", "p", "dd", "td", "article", "section", "div"):
            t = clean_text(cur.text(separator=" "), 400)
            if len(t) > 3:
                return t[:300]
        cur = cur.parent
    return ""


def page_context(tree: HTMLParser) -> str:
    parts = []
    for sel in ("title", "h1", "nav[aria-label*=readcrumb], .breadcrumb, .breadcrumbs, [class*=breadcrumb]"):
        n = tree.css_first(sel)
        if n:
            parts.append(clean_text(n.text(separator=" "), 200))
    return " | ".join(p for p in parts if p)


def extract_links(html: str, base_url: str) -> tuple[list[Link], str]:
    tree = HTMLParser(html)
    base_el = tree.css_first("base[href]")
    base = (normalize_url(base_el.attributes.get("href") or "", base_url) if base_el else "") or base_url
    ctx = page_context(tree)
    links: list[Link] = []
    nodes = tree.css("a[href], area[href], [data-href], [data-url], [data-file], option[value], iframe[src], embed[src], "
                     "object[data], [onclick]")
    heavy = len(nodes) > 2500
    for node in nodes:
        attrs = node.attributes
        raw, origin = None, "a"
        if node.tag in ("a", "area"):
            raw = attrs.get("href")
        elif node.tag == "option":
            raw, origin = attrs.get("value"), "option"
            if not raw or not re.search(r"[/.]", raw):
                continue
        elif node.tag in ("iframe", "embed"):
            raw, origin = attrs.get("src"), "iframe"
        elif node.tag == "object":
            raw, origin = attrs.get("data"), "iframe"
        else:
            raw = attrs.get("data-href") or attrs.get("data-url") or attrs.get("data-file")
            if not raw and attrs.get("onclick"):
                m = _ONCLICK_URL.search(attrs.get("onclick") or "")
                raw, origin = (m.group(1) if m else None), "onclick"
        url = normalize_url(raw or "", base)
        if not url:
            continue
        anchor = clean_text(node.text(separator=" ") or attrs.get("title") or attrs.get("aria-label") or "", 300)
        if not anchor:
            img = node.css_first("img[alt]")
            anchor = clean_text(img.attributes.get("alt") if img else "", 200)
        links.append(Link(url=url, anchor=anchor, heading="" if heavy else _nearest_heading(node),
                          parent="" if heavy else _parent_context(node), origin=origin))
    for m in _URL_IN_TEXT.finditer(html):          # document URLs embedded in inline scripts / JSON blobs
        u = normalize_url(m.group(1), base)
        if u:
            links.append(Link(url=u, origin="script"))
    best: dict[str, Link] = {}
    for l in links:
        k = url_key(l.url)
        cur = best.get(k)
        if cur is None or len(l.anchor) + len(l.heading) > len(cur.anchor) + len(cur.heading):
            best[k] = l
    return list(best.values()), ctx


def needs_js(html: str, n_links: int) -> bool:
    low = html[:300000].lower()
    tree = HTMLParser(html)
    body_text = clean_text(tree.body.text(separator=" ") if tree.body else "")
    markers = ("__next_data__", "ng-version", "data-reactroot", "id=\"root\"", "id=\"app\"", "id=\"__next\"",
               "id=\"___gatsby\"", "window.__nuxt__", "data-v-app", "<app-root", "/sfsites/", "webruntime")
    if n_links < 15 or len(body_text) < 600:
        return True
    if re.search(r"<noscript>[^<]{0,200}(enable|turn on)\s+javascript", low):
        return True
    return any(m in low for m in markers) and n_links < 40


def links_from_json(obj: Any, base: str, out: list[Link], depth: int = 0) -> None:
    """Walk an XHR/JSON payload and pull out document URLs with their titles / dates / ids."""
    if depth > 14 or len(out) > 20000:
        return
    if isinstance(obj, dict):
        title = next((clean_text(str(obj[k]), 300) for k in obj if isinstance(obj[k], str) and
                      re.fullmatch(r"(?i)(title|name|label|display_?name|document_?name|doc_?name|policy_?name|text|heading)", k)), "")
        hints: dict[str, str] = {}
        for k, v in obj.items():
            if not isinstance(v, (str, int)) or isinstance(v, bool):
                continue
            kl = k.lower()
            if "effective" in kl:
                hints["effective_date"] = str(v)
            elif re.search(r"(?:last_?)?(?:updated|modified|revised|review)", kl):
                hints["last_updated_date"] = str(v)
            elif re.search(r"policy_?(?:number|no|num|id|code)", kl):
                hints["policy_number"] = str(v)
        for k, v in obj.items():
            if isinstance(v, str) and len(v) < 600 and " " not in v.strip():
                is_url_key = bool(re.search(r"(?i)(url|href|link|path|file|uri|download|src)", k))
                if v.startswith(("http://", "https://", "/")) and (is_url_key or url_ext(v) in DOC_EXTS):
                    u = normalize_url(v, base)
                    if u and link_kind(u) != "junk":
                        out.append(Link(url=u, anchor=title, origin="xhr", hints=dict(hints)))
            elif isinstance(v, (dict, list)):
                links_from_json(v, base, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:5000]:
            links_from_json(v, base, out, depth + 1)


# =============================================================================
# 10. HTTP FETCHER (robots, rate-limit, retry/backoff, raw-byte hashing, block detection)
# =============================================================================
@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0                # final status after redirects; 0 = transport failure
    first_status: int = 0
    headers: dict = field(default_factory=dict)
    content: bytes = b""           # decoded body (content-encoding removed), at most MAX_PARSE_BYTES - for parsing only
    content_type: str = ""
    sha256: str = ""               # SHA-256 of the exact bytes received on the wire
    size: int = 0                  # length of those same bytes
    truncated: bool = False        # parse buffer shorter than the body (hash/size still cover everything)
    error: str = ""
    blocked: str = ""              # '' or a reason code (see classify_block)
    redirected: bool = False
    via_browser: bool = False
    fetched_at: str = ""
    last_modified: str = ""        # HTTP Last-Modified as YYYY-MM-DD
    content_encoding: str = ""


WAF_SIGNATURES = [   # used on 403 / 429 / 503 responses
    ("cloudflare", re.compile(rb"cf-chl|challenge-platform|cf_chl_opt|just a moment\.\.\.|attention required! \| cloudflare", re.I)),
    ("akamai", re.compile(rb"access denied.{0,400}reference\s*#|bm-verify|akamai", re.I | re.S)),
    ("imperva", re.compile(rb"_incapsula_resource|incapsula incident|request unsuccessful\. incapsula", re.I)),
    ("perimeterx", re.compile(rb"px-captcha|_pxhd|perimeterx|press (?:&|and) hold", re.I)),
    ("datadome", re.compile(rb"datadome|geo\.captcha-delivery", re.I)),
    ("generic_captcha", re.compile(rb"h-captcha|hcaptcha|are you a robot|verify you are (?:a )?human|pardon our interruption", re.I)),
]
WAF_SIGNATURES_200 = [   # 200 responses: only unmistakable interstitials (normal pages can mention akamai / recaptcha)
    ("cloudflare", re.compile(rb"cf-chl|challenge-platform|cf_chl_opt|<title>\s*just a moment", re.I)),
    ("imperva", re.compile(rb"_incapsula_resource", re.I)),
    ("perimeterx", re.compile(rb"px-captcha", re.I)),
    ("datadome", re.compile(rb"geo\.captcha-delivery", re.I)),
    ("generic_captcha", re.compile(rb"pardon our interruption|verify you are (?:a )?human|are you a robot", re.I)),
]
LOGIN_URL_RX = re.compile(r"(?:^|[/._\-?=])(log-?in|sign-?in|signon|sso|auth(?:orize|enticate)?|oauth2?|saml|idp|account/login|"
                          r"logon|b2c_1|myaccount|login\.aspx|identity)(?:[/._\-?=&]|$)", re.I)
_PASSWORD_RX = re.compile(rb"<input[^>]+type=[\"']?password", re.I)
_REG_WALL_RX = re.compile(rb"(?:log ?in|sign ?in|register) to (?:view|access|continue|download)|members? only|"
                          rb"registration (?:is )?required|you must be (?:logged|signed) in|please (?:log|sign) in", re.I)
AUTH_BLOCKS = ("auth_401", "auth_403_login", "login_redirect", "login_wall", "registration_wall")


def is_auth_block(reason: str) -> bool:
    """requires_auth = Y only for a login / portal / registration wall (schema). Bot challenges, bare 403 and 429 are N."""
    return reason in AUTH_BLOCKS


def _visible_text_len(body: bytes) -> int:
    t = re.sub(rb"<(script|style)[^>]*>.*?</\1>", b" ", body[:200000], flags=re.S | re.I)
    return len(clean_text(re.sub(rb"<[^>]+>", b" ", t).decode("utf-8", "ignore")))


def classify_block(status: int, original_url: str, final_url: str, body: bytes) -> str:
    head = body[:60000]
    is_pdf = body[:5].startswith(b"%PDF")
    host = urlparse(original_url).hostname or ""
    is_cdn = is_cdn_host(host)

    if status == 401:
        return "auth_401"
    if status == 407:
        return "proxy_auth_407"
    if status in (403, 429, 503):
        for vendor, rx in WAF_SIGNATURES:
            if rx.search(head):
                return f"bot_challenge_{vendor}_{status}"
        if status == 403:
            if not is_cdn and (_PASSWORD_RX.search(head) or _REG_WALL_RX.search(head)):
                return "auth_403_login"
            return "forbidden_403"
        if status == 429:
            return "rate_limited_429"
    if 200 <= status < 400 and not is_pdf:
        if final_url and url_key(final_url) != url_key(original_url):
            fp = urlparse(final_url)
            if host_has_suffix(fp.hostname or "", IDP_HOST_SUFFIXES) or (
                LOGIN_URL_RX.search(f"{fp.path}?{fp.query}") and not LOGIN_URL_RX.search(urlparse(original_url).path)):
                return "login_redirect"
        if status == 200 and len(body) < 40000:
            for vendor, rx in WAF_SIGNATURES_200:
                if rx.search(head):
                    if not is_cdn:            # a CDN interstitial may still pass to the browser retry / normal parsing
                        return f"bot_challenge_{vendor}_{status}"
        if b"<" in head[:2000]:
            n_links = len(re.findall(rb"<a\s", head, re.I))
            vis = _visible_text_len(head)
            # CDN responses are not login / registration walls (generic "Sign In" footer links give false positives)
            if not is_cdn:
                if _PASSWORD_RX.search(head) and vis < 1500 and n_links < 25:
                    return "login_wall"
                if _REG_WALL_RX.search(head) and vis < 1200 and n_links < 25:
                    return "registration_wall"
    return ""


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """Undo Content-Encoding for parsing only (the hash is always taken over `raw`). Works on partial bodies too."""
    enc = (encoding or "").lower().strip()
    if not raw or enc in ("", "identity"):
        return raw
    try:
        out = raw
        for e in [x.strip() for x in enc.split(",") if x.strip()][::-1]:
            if e in ("gzip", "x-gzip"):
                out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(out)
            elif e == "deflate":
                try:
                    out = zlib.decompressobj().decompress(out)
                except zlib.error:
                    out = zlib.decompressobj(-zlib.MAX_WBITS).decompress(out)
            elif e == "br" and brotli is not None:
                out = brotli.decompress(out)
            else:
                return raw
        return out
    except Exception:
        return raw


class Fetcher:
    def __init__(self, share_with: Optional["Fetcher"] = None) -> None:
        self._owns_client = share_with is None
        self.client = share_with.client if share_with is not None else httpx.Client(
            follow_redirects=True, max_redirects=10,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=15.0),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.8", "Accept-Encoding": "identity", "From": CONTACT},
        )
        # robots cache, per-host politeness clock and their locks are shared between the Fetchers of parallel payers
        src = share_with if share_with is not None else self
        self.robots: dict[str, Optional[Protego]] = src.robots if share_with is not None else {}
        self.robots_status: dict[str, str] = src.robots_status if share_with is not None else {}
        self.last_hit: dict[str, float] = src.last_hit if share_with is not None else {}
        self._lock = src._lock if share_with is not None else threading.Lock()   # protects last_hit (per-host slots)
        self._robots_locks: dict[str, threading.Lock] = src._robots_locks if share_with is not None else {}
        self._pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch")
        self._pre: dict[str, Future] = {}                   # background fetches keyed by URL (main thread only)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_client:
            self.client.close()

    # ---- robots ----
    def robots_for(self, url: str) -> Optional[Protego]:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self.robots:
            return self.robots[origin]
        with self._lock:
            olock = self._robots_locks.setdefault(origin, threading.Lock())
        with olock:
            if origin in self.robots:
                return self.robots[origin]
            return self._load_robots(origin, p)

    def _load_robots(self, origin: str, p) -> Optional[Protego]:
        rp, note = None, "missing"
        try:
            self._wait(p.netloc)
            r = self.client.get(origin + "/robots.txt")
            if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower():
                rp, note = Protego.parse(r.text), "ok"
            elif r.status_code >= 500:
                note = f"robots_{r.status_code}_treated_as_disallow_all"
                rp = Protego.parse("User-agent: *\nDisallow: /")
            else:
                note = f"robots_{r.status_code}_allow_all"
        except httpx.HTTPError as e:
            note = f"robots_error_{type(e).__name__}_allow_all"
        self.robots[origin] = rp
        self.robots_status[origin] = note
        log(logging.DEBUG, "robots_loaded", origin=origin, status=note)
        return rp

    def allowed(self, url: str) -> bool:
        if BYPASS_ROBOTS_FOR_DOCUMENT_URLS:      # opt-in only (see crawler_config): off by default, robots.txt is honoured
            low_url = url.lower()
            if (is_cdn_host(urlparse(url).hostname or "") or url_ext(url) in DOC_EXTS
                    or re.search(r"\.(pdf|docx?|xlsx?|csv|rtf|txt)\b", low_url)
                    or any(h in low_url for h in MAYBE_DOC_HINTS)):
                return True
        rp = self.robots_for(url)
        return True if rp is None else rp.can_fetch(url, USER_AGENT)

    def sitemaps_from_robots(self, url: str) -> list[str]:
        rp = self.robots_for(url)
        return list(rp.sitemaps) if rp else []

    def _delay_for(self, netloc: str) -> float:
        rp = self.robots.get(f"https://{netloc}") or self.robots.get(f"http://{netloc}")
        cd = None
        if rp:
            try:
                cd = rp.crawl_delay(USER_AGENT)
            except Exception:
                cd = None
        return min(max(REQUEST_DELAY_SECONDS, float(cd or 0)), MAX_CRAWL_DELAY_SECONDS)

    def _wait(self, netloc: str) -> None:
        """Per-host politeness gap. Thread-safe: each caller reserves the next free time slot for the host, then sleeps
        outside the lock, so requests to one host stay >= the delay apart while different hosts run in parallel."""
        gap = self._delay_for(netloc)
        with self._lock:
            now = time.monotonic()
            start = max(now, self.last_hit.get(netloc, 0.0) + gap)
            self.last_hit[netloc] = start
        if start > now:
            time.sleep(start - now)

    # ---- background fetching (same fetch(), same robots/politeness rules; only the waiting is overlapped) ----
    def prefetch(self, urls: list[str], keep: str = "") -> None:
        """Start background fetches for `urls`. Entries outside this window are dropped, except `keep` (the item that is
        being processed right now and will still collect its own prefetched result)."""
        want = list(dict.fromkeys(urls))
        for u in [u for u in self._pre if u not in want and u != keep]:          # keep only the current look-ahead window
            self._pre.pop(u).cancel()
        for u in want:
            if u not in self._pre:
                self._pre[u] = self._pool.submit(self.fetch, u)

    def fetch_prefetched(self, url: str) -> FetchResult:
        f = self._pre.pop(url, None)
        if f is not None:
            try:
                return f.result()
            except Exception:       # cancelled / worker error -> do the normal fetch
                pass
        return self.fetch(url)

    def fetch_many(self, urls: list[str], method: str = "GET") -> list[FetchResult]:
        """Fetch several URLs concurrently; results come back in the order of `urls`."""
        futs = [self._pool.submit(self.fetch, u, True, method) for u in urls]
        return [f.result() for f in futs]

    # ---- fetch ----
    def fetch(self, url: str, check_robots: bool = True, method: str = "GET") -> FetchResult:
        res = FetchResult(url=url)
        if check_robots and not self.allowed(url):
            res.error = "robots_disallowed"
            return res
        netloc = urlparse(url).netloc
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait(netloc)
            fetched_at = utc_now_iso()
            try:
                with self.client.stream(method, url) as r:
                    status = r.status_code
                    h = hashlib.sha256()
                    buf = bytearray()
                    total, truncated, aborted = 0, False, False
                    if method == "GET":
                        for chunk in r.iter_raw(65536):            # raw wire bytes: hash before any decoding
                            h.update(chunk)
                            total += len(chunk)
                            room = MAX_PARSE_BYTES - len(buf)
                            if room > 0:
                                buf.extend(chunk[:room])
                            if len(chunk) > room:
                                truncated = True
                            if total > MAX_STREAM_BYTES:
                                aborted = True
                                break
                    enc = r.headers.get("content-encoding", "")
                    body = _decode_body(bytes(buf), enc)
                    final_url = normalize_url(str(r.url)) or str(r.url)
                    if status in RETRY_STATUSES and attempt < MAX_RETRIES and \
                            not classify_block(status, url, final_url, body[:60000]).startswith("bot_challenge"):
                        ra = r.headers.get("retry-after", "")
                        wait = float(ra) if ra.isdigit() else min(60.0, 2.0 ** attempt + 1)
                        log(logging.DEBUG, "retry", url=url, status=status, wait=wait, attempt=attempt)
                        time.sleep(min(wait, 60.0))
                        continue
                    res.status = status
                    res.first_status = r.history[0].status_code if r.history else status
                    res.redirected = bool(r.history)
                    res.final_url = final_url
                    res.headers = {k.lower(): v for k, v in r.headers.items()}
                    res.content_type = res.headers.get("content-type", "")
                    res.content_encoding = enc
                    res.content = body
                    res.truncated = truncated
                    res.fetched_at = fetched_at
                    res.last_modified = http_date_to_iso(res.headers.get("last-modified", ""))
                    if aborted:
                        res.error = "body_exceeds_limit"
                        return res
                    res.size = total
                    res.sha256 = h.hexdigest() if method == "GET" else ""
                    res.blocked = classify_block(status, url, res.final_url, body)
                    return res
            except httpx.ConnectError as e:            # DNS failure / refused: one retry is enough
                res.error, res.fetched_at = type(e).__name__, fetched_at
                if attempt < 2:
                    time.sleep(2.0)
                    continue
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                res.error, res.fetched_at = type(e).__name__, fetched_at
                if attempt < MAX_RETRIES:
                    time.sleep(min(60.0, 2.0 ** attempt))
                    continue
            except httpx.TooManyRedirects:
                res.error, res.fetched_at = "too_many_redirects", fetched_at
                break
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
                res.error, res.fetched_at = f"{type(e).__name__}:{str(e)[:120]}", fetched_at
                break
        return res


# =============================================================================
# 11. HEADLESS BROWSER (Playwright) - JS indexes, tabs, accordions, pagination, dropdowns, XHR capture
# =============================================================================
@dataclass
class RenderResult:
    final_url: str = ""
    status: int = 0
    main_body: bytes = b""                                           # body of the main document response
    snapshots: list[tuple[str, str]] = field(default_factory=list)   # (interaction label, html); '' = on load
    json_payloads: list[tuple[str, Any]] = field(default_factory=list)
    nav_links: list[tuple[str, str, str]] = field(default_factory=list)   # (label, url, option text)
    error: str = ""
    blocked: str = ""
    fetched_at: str = ""


class Browser:
    """Lazy Playwright wrapper. Ordinary headless Chromium, honest UA, no stealth tricks."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self.available = USE_PLAYWRIGHT
        self.renders = 0

    def _start(self) -> bool:
        if self._ctx is not None:
            return True
        if not self.available:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            self._ctx = self._browser.new_context(extra_http_headers={"From": CONTACT}, accept_downloads=False,
                                                  viewport={"width": 1366, "height": 900})
            self._ctx.route("**/*", self._route_filter)
            log(logging.INFO, "browser_started")
            return True
        except Exception as e:
            self.available = False
            log(logging.WARNING, "browser_unavailable", error=str(e)[:200],
                hint="pip install playwright && python -m playwright install chromium")
            return False

    @staticmethod
    def _route_filter(route) -> None:
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            return route.abort()
        if BLOCK_TRACKER_REQUESTS_IN_BROWSER and host_has_suffix(urlparse(req.url).hostname or "", TRACKER_HOST_SUFFIXES):
            return route.abort()
        return route.continue_()

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = None

    @staticmethod
    def _add_snapshot(out: RenderResult, label: str, html: str, seen: set) -> None:
        sig = sha256_hex(html.encode("utf-8", "ignore"))
        if sig in seen or len(out.snapshots) >= MAX_SNAPSHOTS:
            return
        seen.add(sig)
        out.snapshots.append((label, html))

    def render(self, url: str) -> RenderResult:
        out = RenderResult()
        if not self._start():
            out.error = "browser_unavailable"
            return out
        page = self._ctx.new_page()
        responses: list = []
        page.on("response", lambda r: responses.append(r) if r.request.resource_type in ("xhr", "fetch") else None)
        seen: set = set()
        try:
            out.fetched_at = utc_now_iso()
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
            except Exception:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
                page.wait_for_timeout(3000)
            self.renders += 1
            out.status = resp.status if resp else 0
            try:
                out.main_body = resp.body() if resp else b""
            except Exception:
                out.main_body = b""
            out.final_url = normalize_url(page.url) or page.url
            html = page.content()
            out.blocked = classify_block(out.status or 200, url, out.final_url, html.encode("utf-8", "ignore"))
            self._add_snapshot(out, "", html, seen)
            if out.blocked:
                return out
            self._lazy_scroll(page)
            self._add_snapshot(out, "", page.content(), seen)
            self._expand(page, out, seen)
            self._click_tabs(page, out, seen)
            self._paginate(page, out, seen)
            self._walk_selects(page, out, seen)
            for r in responses[:400]:
                try:
                    ct = (r.headers or {}).get("content-type", "")
                    if "json" not in ct.lower() or r.status >= 400:
                        continue
                    body = r.text()
                    if len(body) > 8_000_000:
                        continue
                    out.json_payloads.append((normalize_url(r.url) or r.url, json.loads(body)))
                except Exception:
                    continue
        except Exception as e:
            out.error = f"render_error:{type(e).__name__}:{str(e)[:150]}"
        finally:
            try:
                page.close()
            except Exception:
                pass
        return out

    def _lazy_scroll(self, page) -> None:
        for _ in range(4):
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(400)
            except Exception:
                return

    def _expand(self, page, out: RenderResult, seen: set) -> None:
        clicks = 0
        start_url = page.url
        selectors = ["button[aria-expanded='false']", "[role='button'][aria-expanded='false']",
                     "details:not([open]) > summary", ".accordion-button.collapsed",
                     "[data-toggle='collapse'], [data-bs-toggle='collapse']"]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), MAX_EXPAND_CLICKS)):
                    if clicks >= MAX_EXPAND_CLICKS:
                        break
                    try:
                        loc.nth(i).click(timeout=1500)
                        clicks += 1
                        page.wait_for_timeout(150)
                        if page.url != start_url:
                            page.go_back(wait_until="domcontentloaded")
                    except Exception:
                        continue
            except Exception:
                continue
        if clicks:
            self._add_snapshot(out, f"click:expand-all-accordions({clicks})", page.content(), seen)
        more = re.compile(r"^\s*(load more|show more|view more|show all|view all|expand all|see all|see more)\b", re.I)
        n = 0
        for _ in range(15):
            try:
                btn = page.get_by_role("button", name=more)
                if btn.count() == 0:
                    break
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
                n += 1
            except Exception:
                break
        if n:
            self._add_snapshot(out, f"click:'load more' x{n}", page.content(), seen)

    def _click_tabs(self, page, out: RenderResult, seen: set) -> None:
        try:
            tabs = page.locator("[role='tab']")
            count = min(tabs.count(), MAX_TAB_CLICKS)
        except Exception:
            return
        start_url = page.url
        for i in range(count):
            try:
                t = tabs.nth(i)
                name = clean_text(t.inner_text(timeout=1000), 60) or f"#{i + 1}"
                t.click(timeout=1500)
                page.wait_for_timeout(600)
                self._add_snapshot(out, f"tab:'{name}'", page.content(), seen)
                if page.url != start_url:
                    page.go_back(wait_until="domcontentloaded")
                    tabs = page.locator("[role='tab']")
            except Exception:
                continue

    def _paginate(self, page, out: RenderResult, seen: set) -> None:
        nxt = re.compile(r"^\s*(next|next page|›|»|>)\s*$", re.I)
        for step in range(MAX_PAGINATION_STEPS):
            try:
                cand = page.get_by_role("button", name=nxt)
                if cand.count() == 0:
                    cand = page.get_by_role("link", name=nxt)
                if cand.count() == 0 or not cand.first.is_enabled():
                    return
                before = page.url
                cand.first.click(timeout=2000)
                page.wait_for_timeout(1200)
                html = page.content()
                n_before = len(out.snapshots)
                self._add_snapshot(out, f"pagination:page {step + 2}", html, seen)
                if len(out.snapshots) == n_before:
                    return       # nothing new: last page reached
                if page.url != before:
                    out.nav_links.append((f"pagination:page {step + 2}", page.url, f"page {step + 2}"))
            except Exception:
                return

    def _walk_selects(self, page, out: RenderResult, seen: set) -> None:
        """State / plan pickers: try each option, snapshot what appears, label it select[name]='Option'."""
        try:
            selects = page.locator("select")
            n_sel = min(selects.count(), 4)
        except Exception:
            return
        tried = 0
        for s in range(n_sel):
            try:
                sel = selects.nth(s)
                sel_name = sel.get_attribute("name") or sel.get_attribute("id") or sel.get_attribute("aria-label") or f"select{s + 1}"
                opts = sel.locator("option").evaluate_all(
                    "els => els.map(e => [e.value, (e.textContent || '').trim()]).filter(v => v[0])")
            except Exception:
                continue
            for value, text in opts[:MAX_SELECT_OPTIONS]:
                if tried >= MAX_SELECT_OPTIONS:
                    return
                tried += 1
                label = f"select[{clean_text(sel_name, 40)}]='{clean_text(text, 40) or value}'"
                try:
                    before = page.url
                    sel.select_option(value, timeout=1500)
                    page.wait_for_timeout(700)
                    if page.url != before:
                        out.nav_links.append((label, page.url, text))
                        page.go_back(wait_until="domcontentloaded")
                        selects = page.locator("select")
                        sel = selects.nth(s)
                    else:
                        self._add_snapshot(out, label, page.content(), seen)
                except Exception:
                    continue

    def fetch_bytes(self, url: str) -> FetchResult:
        res = FetchResult(url=url, via_browser=True)
        if not self._start():
            res.error = "browser_unavailable"
            return res
        try:
            res.fetched_at = utc_now_iso()
            r = self._ctx.request.get(url, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS, max_redirects=10)
            body = r.body()
            res.status = res.first_status = r.status
            res.final_url = normalize_url(r.url) or r.url
            res.headers = {k.lower(): v for k, v in r.headers.items()}
            res.content_type = res.headers.get("content-type", "")
            res.content, res.size, res.sha256 = body, len(body), sha256_hex(body)
            res.last_modified = http_date_to_iso(res.headers.get("last-modified", ""))
            res.blocked = classify_block(r.status, url, res.final_url, body)
        except Exception as e:
            res.error = f"browser_fetch_error:{type(e).__name__}"
        return res

