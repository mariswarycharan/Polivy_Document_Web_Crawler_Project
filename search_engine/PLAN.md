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
- SearXNG running locally (WSL2 or Docker - see Decisions), `settings.yml` with JSON enabled, limiter off, a secret key, and a
  hand-picked engine list; a health check that confirms which engines answer.
- Config file: which payers, which domains (taken read-only from the crawler's payer registry), query
  terms, rate limits, output paths.
- Query builder: payer domains x policy terms -> list of queries.
- **Done when:** one payer's queries run against local SearXNG and return JSON results.

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

## Open questions
1. **How to run SearXNG on Windows.** SearXNG supports Linux only, so on Windows it needs either
   WSL2 (a Linux environment built into Windows; SearXNG installed inside it, no Docker) or Docker Desktop
   (which itself runs on WSL2). Which one to use?
2. Which upstream engines are acceptable (DuckDuckGo, Brave, Mojeek, Qwant ...)? Google and Bing do not
   allow automated querying.
