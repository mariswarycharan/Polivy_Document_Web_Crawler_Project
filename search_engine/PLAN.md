# Search Engine Module - Plan

Branch: `search-engine` (created from `crawler-update`; `crawler-update` itself is not changed).
Folder: `search_engine/` - everything for this module lives here until the final integration phase.

## Goal

Find payer policy-document links **independently of the crawler**, store them in a separate Excel file,
compare them with the crawler's output, and keep only the links the crawler missed **that are verified
policy documents** of the requested payer.

## Is SearXNG enough on its own? No - it needs to be combined.

SearXNG is a self-hosted *metasearch* engine: it has no index of its own and forwards each query to
upstream engines (DuckDuckGo, Brave, Mojeek, Google, Bing ...), then merges the results.

| Limitation of SearXNG alone | Effect on this project |
| --- | --- |
| No own index - only what upstream engines return | Deep or unlinked policy PDFs that search engines never ranked are not found |
| Upstream engines rate-limit, suspend or CAPTCHA a single IP | Hundreds of `site:` queries per payer will get engines suspended; results become thin |
| About 10 results per page per engine | Many pages of paging per query; large payers (UHC ~4,000 docs) cannot be covered by search alone |
| `site:` / `filetype:` support differs per engine | Query results are inconsistent across engines |
| Google / Bing terms do not allow automated querying | Keep to engines that tolerate it and keep the volume low |
| JSON API is off by default (needs `formats: [json]`, a `secret_key`, `limiter: false`) | One-time configuration, no problem |
| Results are not guaranteed to be policy documents | Every link still needs verification |

**Chosen combination**

1. **SearXNG (keyword discovery)** - targeted queries per payer: payer domains x policy terms
   ("medical policy", "prior authorization", "formulary", "drug list", "clinical guideline" ...),
   plus drug / code / topic keywords when needed.
2. **Web-archive URL indexes (domain-wide discovery)** - the Common Crawl index and the Internet Archive
   (Wayback) index list every captured URL on a payer's domains, filterable by URL pattern, MIME type
   (PDF) and status. Free, public, no search-engine terms involved. Python library: `cdx_toolkit`.
3. **Verification with the crawler's own checks** - every "search-only" link is fetched once and judged
   with the crawler's existing logic (file-type sniffing, content score, policy evidence, payer ownership /
   brand check, document-type classification). Only links that pass are reported as new policy documents.

## Phases

### Phase 1 - Foundation and SearXNG setup
- Folder layout inside `search_engine/` (config, collectors, compare, verify, output, tests).
- SearXNG running locally in **WSL2 with a minimal Linux (Debian) stored on the E: drive - no Docker**,
  started only when needed. `settings.yml` with JSON enabled, limiter off, a secret key, and a hand-picked
  engine list; a health check that confirms which engines answer.
- Archive-index client (`cdx_toolkit`, pure Python, a few MB) checked against one payer domain.
- Config file: which payers, which domains (taken read-only from the crawler's payer registry), query
  terms, rate limits, output paths.
- Query builder: payer domains x policy terms -> list of queries.
- **Done when:** one payer's queries return JSON results from local SearXNG, and the archive client lists
  URLs for one payer domain.

### Phase 2 - Link collection -> `search_links.xlsx`
- SearXNG collector with paging, polite delays, retry/backoff, and per-engine suspension detection.
- Archive collector (Common Crawl + Wayback via `cdx_toolkit`) filtered to the payer's domains, PDF/Office
  MIME types and policy-like URL paths.
- Normalise every URL with the crawler's `url_key()` and de-duplicate across sources.
- Excel output, one row per link: payer, url, url_key, source (searxng / commoncrawl / wayback),
  engine, query, title, snippet, capture date, found_at.
- **Done when:** `search_links.xlsx` is produced for at least one payer with all three sources.

### Phase 3 - Compare with crawler output and verify
- Load everything the crawler has seen, not only `output.csv`: also rejected, skipped, robots-skipped,
  near-duplicate and blocked lists, so links the crawler already found and rejected do not come back as "new".
- Three groups: found by both, crawler only, **search only**.
- Verify each search-only link with the crawler's fetcher and decision logic -> accepted / rejected /
  unreachable, with reasons.
- For accepted links, record **why the crawler missed it** (never linked, link scored too low, page budget,
  host never seeded, robots, blocked).
- Output `search_gap_analysis.xlsx`: overlap, search-only accepted, search-only rejected, missed-reasons
  summary per payer, plus the overlap % as a recall measure.
- **Done when:** the gap file is produced for one payer and every accepted row has a reason.

### Phase 4 - Integration with the crawler, tests, documentation
- One command to run search -> compare -> verify (e.g. a `--search` option or a separate entry point).
- Final search-engine results stay in their own output files; they are never merged into the crawler's output.
- Tests for query building, URL normalisation, comparison and verification decisions.
- README for the module: setup (Docker, SearXNG), configuration, commands, outputs, limitations.
- **Done when:** a full run for all payers works end to end and the outputs are documented.

## Decisions (13 Nov 2026)
- **Branch:** `search-engine`, committed locally; pushing is done by the team when ready.
- **robots.txt:** verification follows the crawler's own setting on this branch (currently not enforced).
- **Outputs:** the search engine writes its own files (`search_links.xlsx`, `search_gap_analysis.xlsx`, and a
  final verified-links file). They are compared with the crawler output so repeated links are removed, but
  they are **never merged** into the crawler's `output.csv`.
- **Both sources are used:** SearXNG (the search engine agreed by the team) for keyword discovery, plus the
  Common Crawl / Internet Archive indexes for domain-wide discovery.
- **SearXNG setup is kept light** because disk space is limited: WSL2 with a minimal Debian placed on the
  E: drive, no Docker Desktop. Rough footprint 0.5-1.5 GB; nothing runs in the background unless started.

- **Search engine used by SearXNG: DuckDuckGo only**, at a low rate (one request every 6 s). Checked 13 Nov 2026:
  DuckDuckGo's terms and acceptable-use policy do not forbid automated queries or keeping results (they forbid
  disrupting the service and reselling it). Excluded: **Mojeek** (terms forbid automated use and scraping without
  its API), **Brave** (SearXNG reads Brave's web pages, which its licence does not cover; the official API's terms
  forbid storing results and need a credit card), **Google / Bing** (no automated querying).

## Phase status
- **Phase 1 - done (13 Nov 2026).** SearXNG installed in WSL2 on the team machine; `python -m search_engine check`
  answered. Common Crawl and the Internet Archive are reachable. DuckDuckGo answered the first query with a
  CAPTCHA, and SearXNG then pauses it for 24 h; the CAPTCHA is respected, never bypassed.
- **Phase 2 - built, waiting for a first real run.** `python -m search_engine collect --payer <name>` (or `--all`):
  - archive indexes: two filtered passes per domain and source (documents; policy-looking web pages);
  - SearXNG: the next batch of queries (15 by default); the batch stops at the first CAPTCHA, suspension or
    connection error, and the next run continues where it stopped (`output/searxng_progress_<payer>.json`);
  - links stored per payer in `output/links_<payer>.jsonl`, one row per crawler `url_key`, all sources listed;
  - `output/search_links.xlsx` rebuilt after every run: sheet `links` and sheet `summary`.
- **Phase 3 - built (14 Nov 2026).**
  - `python -m search_engine compare --all` (no network): reads the crawler's `output/output.csv` and its checkpoint
    database, and splits every search link into **found by both**, **seen by crawler but not output** (with the
    crawler's status and reason), and **search only** (with why the crawler missed it: host never crawled, crawl not
    finished, or not linked from any visited page).
  - `python -m search_engine verify --payer <name> [--max N]`: fetches each search-only link with the crawler's own
    fetcher and judges it with the crawler's own rules -> accepted / rejected / ambiguous / unreachable. Results are
    kept in `output/verified_<payer>.jsonl`, so runs can be split and resumed.
  - Outputs: `output/search_gap_analysis.xlsx` (summary, new_policy_documents, search_only_other, seen_by_crawler,
    found_by_both) and the final result `output/search_engine_results.xlsx` (verified new policy documents only).
- **Phase 4 - built (14 Nov 2026).** `python -m search_engine run --payer <name> | --all` runs the whole flow:
  optional crawler run (`--crawl`, the crawler is started unchanged as its own process), collection (archive, plus
  `--searxng` for a keyword batch), verification (`--verify-max`, default 200 per payer) and all Excel files.
  Module README written; 30 offline tests. Remaining: first full real run on the team machine.
