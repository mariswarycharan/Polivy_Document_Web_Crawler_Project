"""Settings for the search-engine module. Edit here; environment variables override where noted."""
from __future__ import annotations

import os
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"

# --------------------------------------------------------------------------- SearXNG (keyword search)
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
# Only engines whose terms do not forbid automated queries. Mojeek forbids automated use without its
# API; Brave's API terms forbid storing results; Google and Bing do not allow automated querying.
SEARXNG_ENGINES = ["duckduckgo"]
SEARXNG_DELAY_SECONDS = 6.0      # pause between two SearXNG requests (keeps the upstream engine from suspending us)
SEARXNG_TIMEOUT_SECONDS = 30.0
SEARXNG_MAX_PAGES = 3            # result pages fetched per query (about 10 results each)
SEARXNG_BATCH_QUERIES = 15       # queries per run; the next run continues where this one stopped

# Policy terms combined with every payer domain to build keyword queries.
POLICY_TERMS = [
    "medical policy",
    "coverage policy",
    "clinical policy",
    "clinical guideline",
    "medical necessity",
    "prior authorization",
    "drug policy",
    "formulary",
    "drug list",
    "provider manual",
]
FILE_TYPES = ["pdf"]             # an extra query with filetype:<x> is built for each term

# --------------------------------------------------------------------------- web-archive URL indexes
ARCHIVE_SOURCES = ["cc", "ia"]   # cc = Common Crawl, ia = Internet Archive (Wayback Machine)
ARCHIVE_LIMIT_PER_DOMAIN = 5000  # captures read per domain, source and pass (documents / policy pages)
ARCHIVE_PASS_MAX_MINUTES = 15     # one domain pass gives up after this long and the run moves on (0 = no limit)
ARCHIVE_FROM = ""                # e.g. "2023" to read Common Crawl indexes from 2023 on; "" = the last 12 months
DOCUMENT_MIME_TYPES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")
# An HTML capture is kept only if its path contains one of these (policy pages can be web pages).
POLICY_PATH_HINTS = (
    "polic", "guideline", "criteria", "formular", "drug", "pharmac", "prior-auth", "priorauth",
    "precert", "medical-necessity", "coverage", "clinical", "provider-manual", "bulletin",
)


def user_agent() -> str:
    """Reuse the crawler's identity so both tools present themselves the same way."""
    try:
        from crawler_config import USER_AGENT
        return USER_AGENT
    except Exception:  # pragma: no cover - crawler not importable
        return "PolicyDiscoveryBot/2.0 (+public policy document inventory)"


# ---- Phase 3 verification ----
# Links on these hosts / paths are never policy documents: recorded as rejected without being fetched.
VERIFY_SKIP_HOST_PREFIXES = ["careers.", "jobs.", "job.", "talent."]
VERIFY_SKIP_PATH_HINTS = ["/careers/", "/jobs/", "/job/"]
