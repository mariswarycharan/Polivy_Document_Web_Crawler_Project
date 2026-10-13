#!/usr/bin/env python3
"""Module 1/5 - configuration: INPUT block, SETTINGS, SCHEMA constants, logging and the terminal progress UI.

Part of the Payer Policy Document Discovery Crawler (v2). Logic is unchanged from the single-file version."""
from __future__ import annotations

import threading

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

try:
    from rich.console import Console
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    RICH_AVAILABLE = False


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from crawler_state import StateDB


# =============================================================================
# 1. INPUT  (edit here - no command-line arguments)
# =============================================================================
HOMEPAGE_URL = "https://www.uhcprovider.com"   # the payer homepage / hint host to crawl
PAYER_NAME = ""            # "" -> looked up in PAYER_REGISTRY / payer_seed_list.csv by host
RUN_ALL_PAYERS = False     # True -> ignore HOMEPAGE_URL and crawl every payer in the registry
EXTRA_START_URLS: list[str] = []   # optional extra entry points for the payer in HOMEPAGE_URL
RESET_PAYER_STATE = False  # True -> forget the checkpoint for this payer and start over

# payer_name must be one consistent value per payer (matches the seed list). payer_seed_list.csv,
# if present next to the script, overrides names / hosts. footprint_state is used only for
# single-state payers and only when INFER_STATE_FROM_PAYER_FOOTPRINT is True (noted in `notes`).
# affiliated_domains are owned by the payer and crawled as in-scope. brand_terms prove that a
# document hosted on a non-owned domain really belongs to this payer.
PAYER_REGISTRY: list[dict[str, Any]] = [
    {"payer_name": "UHC", "payer_alias": "UnitedHealthcare", "hint_host": "uhcprovider.com", "footprint_state": "",
     "affiliated_domains": ["uhcprovider.com", "uhc.com", "unitedhealthcare.com", "uhccommunityplan.com", "optumrx.com",
                            "myuhc.com", "uhcmedicaresolutions.com", "unitedhealthcareonline.com"],
     "brand_terms": ["UnitedHealthcare", "United Healthcare", "UHC", "OptumRx"]},
    {"payer_name": "Horizon BCBS NJ", "payer_alias": "Horizon Blue Cross Blue Shield of New Jersey",
     "hint_host": "horizonblue.com", "footprint_state": "NJ",
     "affiliated_domains": ["horizonblue.com", "horizonnjhealth.com", "bravenhealth.com"],
     "brand_terms": ["Horizon Blue Cross", "Horizon BCBSNJ", "Horizon NJ Health", "Horizon Healthcare", "Braven Health"]},
    {"payer_name": "Cigna", "payer_alias": "Cigna Healthcare", "hint_host": "cigna.com", "footprint_state": "",
     "affiliated_domains": ["cigna.com", "cignaforhcp.com", "evernorth.com", "express-scripts.com"],
     "brand_terms": ["Cigna", "Evernorth", "Express Scripts"]},
    {"payer_name": "UPMC Health Plan", "payer_alias": "UPMC Health Plan", "hint_host": "upmchealthplan.com",
     "footprint_state": "PA", "affiliated_domains": ["upmchealthplan.com", "upmc.com"],
     "brand_terms": ["UPMC Health Plan", "UPMC"]},
    {"payer_name": "Elevance / Anthem", "payer_alias": "Anthem Blue Cross and Blue Shield (Elevance Health)",
     "hint_host": "anthem.com", "footprint_state": "",
     "affiliated_domains": ["anthem.com", "elevancehealth.com", "carelonrx.com", "carelon.com", "wellpoint.com",
                            "empireblue.com"],
     "brand_terms": ["Anthem", "Elevance", "CarelonRx", "Carelon", "Wellpoint", "Empire BlueCross"]},
    {"payer_name": "CareSource", "payer_alias": "CareSource", "hint_host": "caresource.com", "footprint_state": "",
     "affiliated_domains": ["caresource.com"], "brand_terms": ["CareSource"]},
    {"payer_name": "Centene", "payer_alias": "Centene Corporation", "hint_host": "centene.com", "footprint_state": "",
     "affiliated_domains": ["centene.com", "ambetterhealth.com", "wellcare.com", "healthnet.com", "superiorhealthplan.com",
                            "sunshinehealth.com", "peachstatehealthplan.com", "buckeyehealthplan.com", "mhsindiana.com",
                            "homestatehealth.com", "pahealthwellness.com", "azcompletehealth.com",
                            "louisianahealthconnect.com", "magnoliahealthplan.com", "nebraskatotalcare.com",
                            "absolutetotalcare.com", "coordinatedcarehealth.com", "cahealthwellness.com",
                            "nhhealthyfamilies.com", "ilmeridian.com", "mimeridian.com"],
     "brand_terms": ["Centene", "Ambetter", "Wellcare", "Health Net", "Envolve"]},
    {"payer_name": "Geisinger", "payer_alias": "Geisinger Health Plan", "hint_host": "geisinger.org",
     "footprint_state": "PA", "affiliated_domains": ["geisinger.org"], "brand_terms": ["Geisinger"]},
    {"payer_name": "Highmark", "payer_alias": "Highmark Blue Cross Blue Shield", "hint_host": "highmark.com",
     "footprint_state": "",
     "affiliated_domains": ["highmark.com", "highmarkbcbs.com", "highmarkbcbswny.com", "highmarkbcbsde.com",
                            "highmarkbcbswv.com", "highmarkhealthoptions.com", "highmarkwholecare.com"],
     "brand_terms": ["Highmark"]},
    {"payer_name": "BCBST (BCBS Tennessee)", "payer_alias": "BlueCross BlueShield of Tennessee",
     "hint_host": "bcbst.com", "footprint_state": "TN", "affiliated_domains": ["bcbst.com"],
     "brand_terms": ["BlueCross BlueShield of Tennessee", "BCBST", "BlueCare Tennessee", "BlueCare"]},
]

# =============================================================================
# 2. SETTINGS
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_LIST_CSV = os.path.join(SCRIPT_DIR, "payer_seed_list.csv")
SCHEMA_DICTIONARY_XLSX = os.path.join(SCRIPT_DIR, "schema_dictionary.xlsx")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")
STATE_DB = os.path.join(OUTPUT_DIR, "crawl_state_v2.db")

CONTACT = "policy-discovery@example.com"   # put a real contact address here
USER_AGENT = f"PolicyDiscoveryBot/2.0 (+public policy document inventory; contact: {CONTACT})"

REQUEST_DELAY_SECONDS = 1.0       # minimum gap between requests to the same host
MAX_CRAWL_DELAY_SECONDS = 10.0    # cap for robots.txt Crawl-delay
REQUEST_TIMEOUT_SECONDS = 40
MAX_RETRIES = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_PARSE_BYTES = 60 * 1024 * 1024      # bytes kept in memory for parsing (the hash always covers the full body)
MAX_STREAM_BYTES = 1024 * 1024 * 1024   # abort pathological downloads above this size

MAX_PAGES_PER_PAYER = 2500        # HTML pages crawled
MAX_DOCUMENTS_PER_PAYER = 20000   # document candidates fetched
MAX_DEPTH = 5                     # link hops from the seed
RELATED_DOMAIN_DEPTH_BUDGET = 3   # hops allowed inside a newly discovered (non-affiliated) related domain
MAX_SITEMAP_URLS = 100000
MAX_SITEMAP_FILES = 400
KNOWN_PATTERN_MIN_DOCS = 50       # run the known-URL-pattern fallback if fewer docs than this

# Gate thresholds (link score units - see score_link)
DOC_CANDIDATE_MIN_SCORE = 3.0     # document-file links at/above this are downloaded + inspected
MAYBE_DOC_MIN_SCORE = 5.0         # extension-less "maybe document" links need more evidence
EXTERNAL_DOC_MIN_SCORE = 5.0      # documents on hosts the payer does not own need more evidence
PAGE_FOLLOW_MIN_SCORE = 2.0       # HTML pages at/above this are crawled
NAV_FOLLOW_MIN_SCORE = 0.5        # depth-1 navigation links from the homepage
RELATED_DOMAIN_MIN_SCORE = 8.5    # off-domain page must score this to open a related domain
HUB_INHERIT_MIN_SCORE = 5.0       # documents on a policy hub page inherit part of the hub's score
HUB_INHERIT_MAX_BONUS = 2.0
UNFETCHED_DOC_ROW_MIN_SCORE = 6.0 # strong candidate that could not be fetched still gets a (non-200) row
REOPEN_REJECTED_SCORE_GAIN = 99.0  # a rejected document is re-evaluated if it re-appears with this much more evidence

INFER_STATE_FROM_PAYER_FOOTPRINT = True   # single-state payers: use footprint_state (noted, -0.05 confidence)
REVISION_POLICY = "latest_only"  # "latest_only": keep the latest version, older/archived versions go to near_duplicates_removed
                                  # "flag": keep every dated revision, flag latest/superseded in notes
                                  # "latest_only": keep the latest, move superseded revisions to near_duplicates_removed
TEXT_FP_MAX_PAGES = 40            # pages used for the normalised-text fingerprint (plus the last 2 pages)
TEXT_FP_MIN_CHARS = 500           # shorter texts get no text fingerprint (too little to compare safely)

# Hosts that serve files for many sites. A document here is accepted only when it is linked from an in-scope page.
CDN_HOST_SUFFIXES = ("azureedge.net", "azurefd.net", "cloudfront.net", "blob.core.windows.net", "akamaized.net",
                     "amazonaws.com", "ctfassets.net", "widen.net", "sitecorecontenthub.cloud", "bynder.com",
                     "brightspotcdn.com", "cdn.prismic.io", "googleusercontent.com",
                     "fastly.net", "edgesuite.net", "edgekey.net", "sitecorecloud.io", "kc-usercontent.com", "cdn.sanity.io")
IDP_HOST_SUFFIXES = ("okta.com", "oktapreview.com", "auth0.com", "b2clogin.com", "login.microsoftonline.com",
                     "onelogin.com", "pingone.com", "pingidentity.com", "forgerock.io")

# ---- sitemap exploration (sitemaps are one discovery channel, never the only one: the link crawl always runs too) ----
SITEMAP_DOC_MIN_SCORE = 1.0       # sitemap document URLs (file extensions) at/above this are queued (was 3.0: opaque URLs were lost);
                                  # Gate 2 (content) still decides, low scores just get a low queue priority
SITEMAP_PAGE_MIN_SCORE = 1.0      # sitemap HTML pages at/above this are queued (native HTML policies often have opaque slugs)
SITEMAP_PAGE_CAP = 3000           # max low-evidence sitemap pages queued per payer (pages scoring >= 3.0 are not counted)
SITEMAP_EXPLORE_AFFILIATED = True # also read robots.txt / sitemaps of the payer's affiliated domains (www.<domain>)
# ---- related domains ----
RELATED_DOMAIN_NO_BRAND_EXTRA = 1.5   # extra score needed when neither the domain nor the link context names the payer
# ---- robots.txt ----
# False (default): robots.txt is honoured for EVERY URL, including document links (the documented behaviour).
# True: URLs that look like documents (.pdf in the path, /content/dam/, /documents/ ...) and CDN hosts skip the robots check.
BYPASS_ROBOTS_FOR_DOCUMENT_URLS = False

# ---- speed-ups (do not change what is crawled or how it is judged; only how the waiting time is used) ----
PARALLEL_PAYERS = 4               # RUN_ALL_PAYERS: payers crawled at the same time (1 = one after another, as before)
FETCH_WORKERS = 6                 # background fetch threads (prefetch + parallel probes). Per-host politeness is still enforced
PREFETCH_AHEAD = 3                # next pending DOCUMENTS fetched in the background while the current document is processed
BLOCK_TRACKER_REQUESTS_IN_BROWSER = True   # headless browser does not load ad / analytics hosts (they only delay "network idle")
TRACKER_HOST_SUFFIXES = ("doubleclick.net", "googletagmanager.com", "google-analytics.com", "googlesyndication.com",
                         "facebook.net", "hotjar.com", "clarity.ms", "fullstory.com", "quantserve.com",
                         "scorecardresearch.com", "adsrvr.org", "nr-data.net", "segment.io", "segment.com")
USE_PLAYWRIGHT = True
BROWSER_RETRY_ON_BOT_CHALLENGE = True   # one ordinary headless render (no stealth) when plain HTTP gets a JS challenge
PLAYWRIGHT_NAV_TIMEOUT_MS = 45000
MAX_EXPAND_CLICKS = 60
MAX_TAB_CLICKS = 20
MAX_PAGINATION_STEPS = 25
MAX_SELECT_OPTIONS = 60
MAX_SNAPSHOTS = 80
MAX_RENDERS_PER_PAYER = 400       # headless renders per payer (RAM/time guard)

USE_LLM = True
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b"       # Qwen2.5 3B instruct (CPU, ~2 GB RAM in Q4)
LLM_TIMEOUT_SECONDS = 180         # first call also loads the model
LLM_NUM_PREDICT = 400             # enough room that the JSON object is never cut off
LLM_NUM_CTX = 4096
LLM_MAX_ATTEMPTS = 3              # schema-constrained -> JSON mode -> free text with a correction message
MAX_LLM_CALLS_PER_PAYER = 600
LLM_FOR_MISSING_FIELDS = True     # also ask the LLM when document type / line of business stay unknown

USE_OCR = True                    # Tesseract OCR for scanned PDFs (only if pytesseract is installed)
SAVE_DOCUMENT_FILES = False       # True -> keep a copy of every accepted document under output/files

PROGRESS_BAR = True               # live progress bar in the terminal (rich); plain status lines if not a TTY
PROGRESS_REFRESH_SECONDS = 1.0
PLAIN_STATUS_EVERY_SECONDS = 20
CONSOLE_LOG_LEVEL = logging.INFO  # the JSON log file always gets DEBUG

# =============================================================================
# 3. SCHEMA - 22 columns, exact names and order (verified against schema_dictionary.xlsx at start-up)
# =============================================================================
COLUMNS = [
    "payer_name", "payer_alias", "state_or_region", "line_of_business", "document_title",
    "document_type", "document_url", "source_page_url", "discovery_path", "file_type",
    "policy_number", "effective_date", "last_updated_date", "http_status",
    "content_hash_sha256", "file_size_bytes", "requires_auth", "render_mode",
    "extraction_method", "confidence_score", "scrape_timestamp_utc", "notes",
]
ENUMS: dict[str, list[str]] = {
    "line_of_business": ["Commercial", "Medicare", "Medicaid", "Exchange", "Federal", "Multiple", "Unknown"],
    "document_type": ["medical_policy", "pharmacy_policy", "coverage_guideline", "pa_form", "pa_list", "formulary",
                      "drug_list", "provider_manual", "bulletin", "other"],
    "file_type": ["html", "pdf", "doc", "docx", "xls", "xlsx", "other"],
    "requires_auth": ["Y", "N"],
    "render_mode": ["static", "headless", "api"],
    "extraction_method": ["css_selector", "xpath", "regex", "json_api", "sitemap", "pdf_text", "pdf_annotation",
                          "url_pattern", "manual"],
}
STATE_NATIONAL = "National"
STATE_JOINER = "|"
NOTES_MAX_CHARS = 600

# =============================================================================
# 4. LOGGING + TERMINAL PROGRESS
# =============================================================================
LOGGER = logging.getLogger("payer_crawler")
STOP = threading.Event()   # set by main() on Ctrl+C so parallel payer workers stop at the next safe point


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": utc_now_iso(), "level": record.levelname, "event": record.getMessage()}
        d.update(getattr(record, "fields", {}) or {})
        return json.dumps(d, ensure_ascii=False, default=str)


def _console_text(record: logging.LogRecord) -> str:
    f = getattr(record, "fields", {}) or {}
    extra = " ".join(f"{k}={str(v)[:110]}" for k, v in f.items() if k != "trace")
    return f"{datetime.now().strftime('%H:%M:%S')} {record.levelname:<7} {record.getMessage()} {extra}".rstrip()


class _ConsoleHandler(logging.Handler):
    """Prints through the progress UI so log lines appear above the live bars instead of breaking them."""

    def __init__(self, ui: "ProgressUI") -> None:
        super().__init__()
        self.ui = ui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ui.print(_console_text(record))
        except Exception:  # pragma: no cover
            self.handleError(record)


def setup_logging(ui: "ProgressUI") -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()
    LOGGER.propagate = False
    fh = logging.FileHandler(os.path.join(LOG_DIR, "crawl.jsonl"), encoding="utf-8")
    fh.setFormatter(_JsonFormatter())
    fh.setLevel(logging.DEBUG)
    ch = _ConsoleHandler(ui)
    ch.setLevel(CONSOLE_LOG_LEVEL)
    LOGGER.addHandler(fh)
    LOGGER.addHandler(ch)


def log(level: int, event: str, **fields: Any) -> None:
    LOGGER.log(level, event, extra={"fields": fields})


PAGE_DONE_STATES = ("done", "blocked", "failed", "skipped_robots", "skipped_budget")
DOC_DONE_STATES = ("accepted", "rejected", "blocked", "failed", "skipped_robots", "skipped_budget")


class ProgressUI:
    """Live terminal progress:

        Payer 3/10 . Cigna        [bar]  3/10  0:42:10   phase: crawl . /providers/medical-policies
          Pages crawled           [bar]  412/980  ...    queue 568 . blocked 2 . failed 7
          Documents checked       [bar] 1203/2950 ...    accepted 1010 . rejected 160 . blocked 4 . failed 29 . queue 1747

    Totals grow while new links are discovered, so bars can move back a little - that is expected.
    Without a terminal (output piped to a file) a plain status line is printed every PLAIN_STATUS_EVERY_SECONDS.
    """

    # per-thread view (payer, phase, current URL, status-line clock) so payers crawled in parallel do not overwrite each other
    @staticmethod
    def _tl_prop(name: str, default: Any) -> property:
        return property(lambda self: getattr(self._tl, name, default), lambda self, v: setattr(self._tl, name, v))

    payer = _tl_prop.__func__("payer", "")
    payer_idx = _tl_prop.__func__("payer_idx", 0)
    cur_phase = _tl_prop.__func__("cur_phase", "")
    current = _tl_prop.__func__("current", "")
    _last_plain = _tl_prop.__func__("_last_plain", 0.0)

    def __init__(self) -> None:
        self._tl = threading.local()
        self.n_payers = 1
        self.console = Console() if RICH_AVAILABLE else None
        self.use_rich = bool(PROGRESS_BAR and RICH_AVAILABLE and self.console is not None and self.console.is_terminal)
        self.progress = None
        self.payer = ""
        self.payer_idx = 0
        self.cur_phase = ""
        self.current = ""
        self._last = 0.0
        self._last_plain = 0.0
        self.t_payer = self.t_pages = self.t_docs = None

    def start(self, n_payers: int) -> None:
        self.n_payers = max(1, n_payers)
        if not self.use_rich:
            return
        self.progress = Progress(
            TextColumn("{task.description}"), BarColumn(bar_width=30), MofNCompleteColumn(),
            TimeElapsedColumn(), TextColumn("{task.fields[info]}"),
            console=self.console, refresh_per_second=4, transient=False)
        self.progress.start()
        self.t_payer = self.progress.add_task("Payers", total=self.n_payers, info="")
        self.t_pages = self.progress.add_task("  Pages crawled", total=1, info="")
        self.t_docs = self.progress.add_task("  Documents checked", total=1, info="")

    def print(self, text: str) -> None:
        if self.progress is not None:
            self.progress.console.print(text, markup=False, highlight=False, soft_wrap=True)
        else:
            print(text, flush=True)

    def payer_started(self, idx: int, name: str) -> None:
        self.payer, self.payer_idx, self.cur_phase, self.current = name, idx, "start", ""
        if self.progress is not None:
            self.progress.update(self.t_payer, description=f"Payer {idx}/{self.n_payers} · {name}",
                                 completed=idx - 1, info="phase: start")
            self.progress.reset(self.t_pages, total=1, completed=0, info="")
            self.progress.reset(self.t_docs, total=1, completed=0, info="")

    def payer_finished(self, idx: int) -> None:
        if self.progress is not None:
            self.progress.update(self.t_payer, completed=idx, info="phase: done")

    def phase(self, name: str) -> None:
        self.cur_phase = name
        if self.progress is not None:
            self.progress.update(self.t_payer, info=f"phase: {name}")

    def update(self, db: "StateDB", payer: str, current_url: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if current_url:
            self.current = current_url
        if not force and now - self._last < PROGRESS_REFRESH_SECONDS:
            return
        self._last = now
        c = db.status_counts(payer)
        g = lambda kinds, states: sum(c.get((k, s), 0) for k in kinds for s in states)
        D = ("doc", "maybe_doc")
        pages_done = g(("page",), PAGE_DONE_STATES)
        pages_q = g(("page",), ("pending", "in_progress"))
        docs_done = g(D, DOC_DONE_STATES)
        docs_q = g(D, ("pending", "in_progress"))
        acc, rej, blk, fail = g(D, ("accepted",)), g(D, ("rejected",)), g(D, ("blocked",)), g(D, ("failed",))
        cur = urlparse(self.current).path[-60:] if self.current else ""
        if self.progress is not None:
            self.progress.update(self.t_pages, completed=pages_done, total=max(1, pages_done + pages_q),
                                 info=f"queue {pages_q} · blocked {g(('page',), ('blocked',))} · failed {g(('page',), ('failed',))}")
            self.progress.update(self.t_docs, completed=docs_done, total=max(1, docs_done + docs_q),
                                 info=f"accepted {acc} · rejected {rej} · blocked {blk} · failed {fail} · queue {docs_q}")
            self.progress.update(self.t_payer, info=f"phase: {self.cur_phase}" + (f" · {cur}" if cur else ""))
        elif force or now - self._last_plain >= PLAIN_STATUS_EVERY_SECONDS:
            self._last_plain = now
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.payer} ({self.payer_idx}/{self.n_payers}) "
                  f"phase={self.cur_phase} | pages {pages_done}/{pages_done + pages_q} | docs {docs_done}/{docs_done + docs_q} "
                  f"(accepted {acc}, rejected {rej}, blocked {blk}, failed {fail})", flush=True)

    def stop(self) -> None:
        if self.progress is not None:
            try:
                self.progress.stop()
            except Exception:  # pragma: no cover
                pass
            self.progress = None

