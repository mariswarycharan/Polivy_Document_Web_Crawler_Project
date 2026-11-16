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

# ---- Strict review (search engine only; runs after the crawler's own rules, never changes the crawler) ----
# Accepted / ambiguous links whose title or file name matches one of these are rejected. Label -> regex.
STRICT_REJECT_PATTERNS = {
    "HIPAA authorization to release records (not a prior-auth form)":
        r"(member|enrollee|patient)[\s_-]*authori[sz]ation|authori[sz]ation[\s_-]*(form)?[\s_-]*hipaa|hipaa[\s_-]*authori[sz]ation",
    "privacy / nondiscrimination / language notice":
        r"privacy|non[\s_-]?discrimination|nopp\b|substitute[\s_-]*notice|subsitute|interpreter|tagline",
    "corporate / ESG / financial report":
        r"impact[\s_-]*report|environmental[\s_-]*polic|sustainability|tcfd|assurance[\s_-]*statement|earnings|"
        r"gender[\s_-]*pay|annual[\s_-]*report|investor",
    "marketing / case study / research":
        r"case[\s_-]*study|casestudy|e-?book|white[\s_-]*paper|(?:^|[\s/])wp-|infographic|newsletter|flyer|partnership[\s_-]*profile|"
        r"druginsights|public[\s_-]*policy[\s_-]*institute|ppi_|brg_|journey",
    "technical / API documentation": r"\bapi\b|api[\s_-]*doc|interoperability|developers?[\s_-]*portal",
    "member handbook / plan summary": r"member[\s_-]*handbook|\bsbc\b|summary[\s_-]*of[\s_-]*benefits",
    "statistics report (prior-auth metrics)": r"priorauths?[\s_-]*metrics|prior[\s_-]*auths?[\s_-]*metrics",
    "member HIPAA-rights form (records, disclosures, restrictions)":
        r"access[\s_-]*to[\s_-]*records|accounting[\s_-]*of[\s_-]*disclosures|amendment[\s_-]*of[\s_-]*records|"
        r"restriction[\s_-]*of[\s_-]*information|cancel[\s_-]*restrictions?|confidential[\s_-]*communications?|"
        r"cancel[\s_-]*authori[sz]ation|advance[\s_-]*directive",
    "member rights / appeal / grievance material":
        r"member[\s_-]*rights|rights[\s_-]*(and|&)?[\s_-]*responsibilit|member[\s_-]*appeal|appeal[\s_-]*request|"
        r"complaints?[\s_-]*(and|&)?[\s_-]*appeals?|grievance|notice[\s_-]*of[\s_-]*action|noticeaction",
    "member letter template": r"coverage[\s_-]*approved|not[\s_-]*medically[\s_-]*necessary[\s_-]*letter|approval[\s_-]*letter|denial[\s_-]*letter",
    "language-assistance / civil-rights notice":
        r"language[\s_-]*assistance|hhs[\s_-]*notice|civil[\s_-]*rights|\d{6,}[a-z]{2,3}(en|sp)(wlp|mub)(?![a-z])",
    "member guide / program summary / compliance notice":
        r"member[\s_-]*guide|memberguide|quality[\s_-]*improvement[\s_-]*program|false[\s_-]*claims|overview[\s_-]*of",
    "benefit summary / plan comparison (member material)":
        r"benefits?[\s_-]*summary|plan[\s_-]*benefits|compare[\s_-]*plan|plan[\s_-]*compar|evidence[\s_-]*of[\s_-]*coverage|"
        r"coverkids[\s_-]*member",
    "fair hearing / external review request (member appeal)":
        r"fair[\s_-]*hearing|external[\s_-]*review|federal[\s_-]*review",
    "survey / report / fact sheet / directory":
        r"cahps|survey[\s_-]*result|executive[\s_-]*summary|fact[\s_-]*sheet|factsheet|flier|directory|rxchoice",
    "system / claims-submission notice (not policy)":
        r"claim[\s_-]*form|moneygram|power[\s_-]*acc|contributions|discharge[\s_-]*form|treatment[\s_-]*report|"
        r"path[\s_-]*toward|cdt[\s_-]*\d{2}[\s_-]*updates|\bevv\b|_evv|evv_|evv[a-z]|training[\s_-]*guide|companion[\s_-]*(document|guide)|837[ip]?\b",
    "premium rate filing (not coverage policy)": r"rate[\s_-]*justification|ratejustification|rate[\s_-]*fil|ratereview",
    "HEDIS / quality-measure coding guide (not coverage policy)": r"hedis|ecds|coding[\s_-]*booklet",
    "member newsletter / engagement / one-pager":
        r"make[\s_-]*health[\s_-]*happen|digital[\s_-]*engagement|one[\s_-]?pager|onepager|dental[\s_-]*benefits",
    "corporate compliance / fraud policy (not coverage policy)":
        r"compliance[\s_-]*investigation|complianceinvestigation|fraud|waste[\s_-]*fraud",
    "appeal form / appeal rights": r"appeal",
    "member notice / certificate / plan-change letter":
        r"certificate|plan[\s_-]*changes|planchanges|preventive[\s_-]*care[\s_-]*schedule|needs[\s_-]*new[\s_-]*providers|"
        r"health[\s_-]*advisory|designation[\s_-]*of[\s_-]*representative",
    "provider admin form / guide (not prior authorization)":
        r"talking[\s_-]*points|overpayment|critical[\s_-]*incident|conflict[\s_-]*of[\s_-]*interest|instruction[\s_-]*letter|"
        r"referral[\s_-]*form|tutorial|quick[\s_-]*ref|quickref|qrg\b|qrg\.|annual[\s_-]*review[\s_-]*report|glide[\s_-]*path|"
        r"territory[\s_-]*listing|availity",
    "policy-site boilerplate (definitions / disclaimer page)": r"/(definitions|disclaimer)(/|\s|$)",
    "FAQ / announcement / news / policy brief":
        r"\bfaq|faq\b|faq\.|announcement|policy[\s_-]*brief|provider[\s_-]*news|providernews_|innovation|civicascript",
    "emergency / COVID notice": r"covid|state[\s_-]*of[\s_-]*emergency|stateofemergency|proclamation",
    "training / presentation / program promotion":
        r"training[\s_-]*academy|presentation|recognition[\s_-]*program|healthy[\s_-]*rewards|legislative[\s_-]*report|"
        r"nadac|regional[\s_-]*manager[\s_-]*map|patient[\s_-]*360|comparison[\s_-]*chart",
    "member / provider administrative form (not prior authorization)":
        r"pcp[\s_-]*change|change[\s_-]*request|provider[\s_-]*application|ohi[\s_-]*form|other[\s_-]*health[\s_-]*insurance|"
        r"notification[\s_-]*of[\s_-]*delivery|attestation",
}
# Phrases in the AI's own reason that mean "no", whatever its yes/no field says.
AI_SAYS_NO_PATTERNS = (r"\bnot\s+(a|an)?\s*([a-z/&-]+\s+){0,4}(polic|guideline)|\bno\s+policy|"
                       r"content\s+(is\s+)?not\s+(provided|available|shown|clear)|not\s+(directly|clearly)\s+(a\s+)?polic")
# Accepted links are moved to "ambiguous" (for the local AI / a person) when the evidence is thin:
STRICT_MIN_CONFIDENCE_FOR_OTHER = 0.80   # document type "other" needs at least this confidence
STRICT_MIN_LINK_SCORE = 0.0              # link / title score below this = the title does not look like a policy

# ---- Local AI (Phase 3c) ----
AI_PROMPT_VERSION = 2          # raise this to have the AI decide again on links it settled with an older prompt
# Abbreviations in payer file names, spelled out for the AI (file name word -> meaning). Matched as whole words.
AI_FILENAME_ABBREVIATIONS = {
    "cumg": "clinical utilization management guidelines", "cpg": "clinical practice guidelines",
    "um": "utilization management", "pa": "prior authorization", "precert": "precertification",
    "mp": "medical policy", "cg": "clinical guideline", "rp": "reimbursement policy", "pdl": "preferred drug list",
    "caid": "medicaid", "dsnp": "dual special needs plan", "snp": "special needs plan",
    "bh": "behavioral health", "ltss": "long-term services and supports", "hcbs": "home and community based services",
    "nopp": "notice of privacy practices", "sbc": "summary of benefits and coverage",
}
# Language versions of one document (file names ending in these) get one shared AI decision.
LANGUAGE_SUFFIXES = ["english", "eng", "en", "spanish", "spa", "spn", "sp", "es", "arabic", "ar", "french", "fr",
                     "chinese", "zh", "korean", "ko", "russian", "ru", "vietnamese", "vi"]
