# Search engine module

Finds payer policy-document links **independently of the crawler**, compares them with what the crawler found,
and checks the links only the search found. Its results stay in their own files; they are never merged into
the crawler's `output.csv`.

## How it works

1. **Collect** links for each payer in the crawler's registry (`crawler_config.PAYER_REGISTRY`, read only):
   - **Web-archive indexes** (main source): Common Crawl and the Internet Archive list captured URLs on the
     payer's domains. Two filtered passes per domain: document files (PDF/Word/Excel) and policy-looking web pages.
   - **SearXNG** (keyword search, DuckDuckGo engine only): queries such as `site:centene.com "medical policy" filetype:pdf`,
     run in small batches. A batch stops at the first CAPTCHA or suspension and the next run continues from there.
2. **Compare** with the crawler: every link is *found by both*, *seen by the crawler but not output* (with the
   crawler's reason), or *search only* (with why the crawler missed it).
3. **Verify** the search-only links with the crawler's own fetcher and decision rules: accepted, rejected,
   ambiguous (needs a human look) or unreachable.

## Setup

```powershell
python -m pip install -r search_engine\requirements.txt
```
SearXNG is optional (only for keyword search): see `setup\SETUP_SEARXNG_WSL2.md` (WSL2, no Docker).

## Commands (run from the project folder)

| Command | What it does |
| --- | --- |
| `python -m search_engine run --payer Centene` | Whole flow: collect (archive), compare, verify 200 links, write the Excel files |
| `python -m search_engine run --all --crawl --searxng --verify-max -1` | Crawler first, archive + SearXNG batch, verify everything, all payers |
| `python -m search_engine collect --all --source archive` | Collect only |
| `python -m search_engine compare --all` | Compare only (no internet) |
| `python -m search_engine verify --payer Centene --max 200` | Verify the next 200 search-only links |
| `python -m search_engine check` | Is SearXNG answering, are the archive indexes reachable? |
| `python -m search_engine queries --payer Centene` | Show the SearXNG queries for a payer |

`--crawler-output <folder>` compares with a different crawler output folder (default: this project's `output\`).
Every step saves as it goes; running a command again continues where it stopped and never duplicates links.

## Outputs (`search_engine\output\`)

| File | Contents |
| --- | --- |
| `search_links.xlsx` | Every link found, with its sources; plus a per-payer summary |
| `search_gap_analysis.xlsx` | Summary per payer (overlap %, why links were missed) and one sheet per group |
| `search_engine_results.xlsx` | **Final result:** verified policy documents the crawler did not find |
| `links_<payer>.jsonl`, `verified_<payer>.jsonl`, `searxng_progress_<payer>.json` | Working data for resuming |

## Known limits

- **DuckDuckGo may answer with a CAPTCHA.** It is respected, never bypassed; SearXNG then pauses the engine for
  24 h. Keyword search is therefore a best-effort extra; the archive indexes do the bulk of the work.
- **The archive index servers can time out** on very large domains. After 3 failures in a row the domain pass is
  skipped and reported, and the run moves on.
- **Archive results can be old.** A captured URL may no longer exist; verification marks it *unreachable*.
- **robots.txt** follows the crawler's own setting on this branch (currently not enforced).
- Verification uses the crawler's rule-based checks only; the crawler's local LLM is not called, so unclear
  cases are reported as *ambiguous* for a person to review.

## Verification: strict review and local AI

`verify` checks each search-only link with the crawler's own fetcher and rules, then:

1. **Skips** careers / jobs links without fetching (`VERIFY_SKIP_*` in `config.py`).
2. **Strict review** (search engine only): rejects authorization-to-release (HIPAA) forms, notices, corporate /
   ESG / earnings reports, marketing, API docs, handbooks and prior-auth statistics; moves thin accepts to
   ambiguous (`STRICT_*` in `config.py`). Re-applied to saved results on every load - no re-fetching.
3. **Local AI** for ambiguous links: the crawler's own `LLMClassifier` (Ollama, `OLLAMA_MODEL` in
   `crawler_config.py`) with the crawler's Gate 3 decision (AI confirms >= 0.5 -> accepted; otherwise the rule
   override, otherwise rejected). New ambiguous links are decided straight away; ambiguous links from earlier runs
   are fetched again and decided after the new ones. Answers are cached in
   `output/llm_cache_search_engine.jsonl` (the crawler's database is never written). `--no-ai` turns it off.

The Excel files show `crawler_verdict` / `crawler_reason` (before strict review) and the `ai_*` columns.
