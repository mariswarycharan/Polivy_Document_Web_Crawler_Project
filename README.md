# Payer Policy Document Discovery Crawler

Crawls a health-payer website (UnitedHealthcare, Cigna, Anthem, …), finds its public policy documents (medical policies,
pharmacy policies, PA forms/lists, formularies, …) and writes an inventory (CSV + Excel) following the 22-column schema in
`schema_dictionary.xlsx`.

It combines plain HTTP crawling (`httpx`), a headless browser for JavaScript-heavy pages (Playwright + Chromium), PDF
parsing (PyMuPDF), optional OCR for scanned PDFs (Tesseract) and an optional local LLM (Ollama) that makes the final call on
borderline documents.

## 1. What you need to install

| Component | Required? | Installed with | Used for |
|---|---|---|---|
| Python 3.10+ (developed on 3.12) | yes | python.org / package manager | everything |
| Python packages | yes | `pip install -r requirements.txt` | everything |
| Chromium for Playwright | yes for JS-rendered sites (`USE_PLAYWRIGHT = True`) | `python -m playwright install ...` | rendering pages that need JavaScript |
| Tesseract OCR | optional (`USE_OCR = True`) | OS installer, **not pip** | text from scanned PDFs |
| Ollama + `qwen2.5:3b` | optional (`USE_LLM = True`) | ollama.com, **not pip** | LLM classification of borderline documents |

The crawler starts even if the optional pieces are missing: without Ollama or the model it logs `llm_unavailable` /
`llm_model_missing` and carries on without the LLM; without Tesseract, scanned PDFs get a note "OCR unavailable"; if Chromium
is missing it logs `browser_unavailable` and crawls without rendering. Results are better with all three, so install them.

Rough resources: >2 GB RAM/VRAM for the LLM model, >400 MB disk for Chromium, >2 GB disk for the model download.

## 2. Setup from scratch

Open a terminal in the project folder first (`cd Polivy_Document_Web_Crawler_Project`).

### 2.1 Python and virtual environment

**Linux (Debian/Ubuntu)**
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 -m venv .venv
source .venv/bin/activate
```

**macOS** (Homebrew from https://brew.sh)
```bash
brew install python git
python3 -m venv .venv
source .venv/bin/activate
```

**Windows** (PowerShell) – install Python 3.10+ from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```
If PowerShell refuses to run the activation script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and retry
(or use `cmd.exe` and `.venv\Scripts\activate.bat`).

Then, on every OS (with the venv active):
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```
On Windows use `python` (or `py`) in all commands below; on Linux/macOS use `python3` if `python` is not found outside the venv.

### 2.2 Playwright Chromium (with system dependencies)

```bash
python -m playwright install --with-deps chromium
```
- **Linux:** `--with-deps` also installs the system libraries Chromium needs via `apt`, so it needs root – run it with
  `sudo` if you are not root, e.g. `sudo .venv/bin/python -m playwright install --with-deps chromium`
  (note that this puts the browser in root's cache; alternatively run `sudo .venv/bin/python -m playwright install-deps chromium`
  for the libraries, then `python -m playwright install chromium` as your normal user). Playwright's automatic dependency
  install supports Debian/Ubuntu; on other distributions install the libraries manually
  (see https://playwright.dev/python/docs/browsers).
- **macOS / Windows:** the system libraries are already present; `python -m playwright install chromium` is enough
  (`--with-deps` is harmless there).
- Re-run this command after upgrading the `playwright` package, because each release expects its own browser build.

### 2.3 Tesseract OCR (optional, for scanned PDFs)

`pytesseract` (installed by pip) is only a wrapper. The real Tesseract program must be installed separately.

**Linux (Debian/Ubuntu)**
```bash
sudo apt install -y tesseract-ocr
```
**macOS**
```bash
brew install tesseract
```
**Windows** – there is no official Windows binary from the Tesseract project; use the community build from UB Mannheim
(https://github.com/UB-Mannheim/tesseract/wiki):
1. Download and run the 64-bit installer (`tesseract-ocr-w64-setup-<version>.exe`) and keep English selected.
2. Note the install folder. The usual location is `C:\Program Files\Tesseract-OCR\`.
3. **Add that folder to your `PATH`** (Start → "Edit the system environment variables" → Environment Variables → Path → New).
   The installer's own "add to PATH" option is off by default and the maintainers recommend leaving it off, so do this
   manually. `pytesseract` finds Tesseract only through `PATH`, and this project does not set a path in code.
4. Open a **new** terminal and check it works:
```bash
tesseract --version
```
If you do not want to touch `PATH`, you can instead add `pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"`
inside `_ocr_first_page()` in `crawler_io.py`.

To skip OCR entirely set `USE_OCR = False` in `crawler_config.py`.

### 2.4 Ollama and the LLM model (optional, for LLM classification)

Ollama runs a local server on `http://localhost:11434`.

**Linux**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```
The script sets Ollama up as a background service (systemd). If it is not running, start it with `ollama serve`.

**macOS** – install the app from https://ollama.com/download (it runs in the menu bar), or `brew install ollama` and then
run `ollama serve` (or `brew services start ollama`) in a separate terminal.

**Windows** – download the installer from https://ollama.com/download (or `winget install Ollama.Ollama`). It starts
Ollama in the background automatically.

Then, on every OS, download the model the crawler is configured for (about 2 GB):
```bash
ollama pull qwen2.5:3b
ollama list          # qwen2.5:3b must be listed
```
The model name must match `OLLAMA_MODEL` in `crawler_config.py`. At start-up the crawler checks `/api/tags` and disables the
LLM (with a warning in the log) if the server is not reachable or the model is not pulled. The first LLM call is slow because
the model is loaded into memory (`LLM_TIMEOUT_SECONDS = 180`).

To run without an LLM set `USE_LLM = False`.

### 2.5 Quick check

```bash
python -c "import payer_policy_crawler; print('imports ok')"
tesseract --version      # optional
ollama list              # optional
```

## 3. Running the crawler

There are **no command-line arguments**. All input and settings are Python constants at the top of `crawler_config.py`. Edit
the file, save it, then run:
```bash
python payer_policy_crawler.py
```
- Progress is shown as live bars (via `rich`) in the terminal; in a non-interactive terminal a status line is printed every
  20 s.
- **Ctrl+C** stops safely. The crawl state is checkpointed in an SQLite file, so **running the same command again resumes**
  where it stopped. Output files are (re)written at the end of every run, including after Ctrl+C.
- To start a payer from scratch set `RESET_PAYER_STATE = True` for one run (then set it back to `False`), or delete
  `output/crawl_state_v2.db`.

### Output (`output/`)

| File | Content |
|---|---|
| `output.csv`, `output.xlsx` | the inventory of all crawled payers, 22 columns |
| `policy_documents_<payer>.csv/.xlsx` | the same, one pair per payer |
| `run_summary.csv` | per-payer counts (attempted, found, failed, blocked, duplicates removed, reconciliation) |
| `skipped_doc_links.csv` | candidates that were not collected, with the reason (low score, excluded type, older revision, duplicate) |
| `rejected_candidates.csv` | documents that were inspected and rejected, with scores and reason |
| `near_duplicates_removed.csv`, `revision_audit.csv` | de-duplication and "latest revision only" decisions |
| `blocked_pages.csv` | pages blocked by login walls / bot protection |
| `hub_coverage.csv` | policy listing pages that declare a document count, compared with what was found |
| `logs/crawl.jsonl` | full JSON log (DEBUG level) |
| `crawl_state_v2.db` | SQLite checkpoint (resume state) |
| `files/<payer>/…` | copies of accepted documents, only if `SAVE_DOCUMENT_FILES = True` |

## 4. Customising the crawl (`crawler_config.py`)

Everything below is in `crawler_config.py`. Only the first block is needed for normal use.

### 4.1 Section 1 – INPUT (what to crawl)

| Setting | Meaning |
|---|---|
| `HOMEPAGE_URL` | Start URL of the payer, e.g. `"https://www.cigna.com"`. The host is matched against `PAYER_REGISTRY` to get the payer's name, alias and domains. |
| `PAYER_NAME` | Override the payer name written to the output. `""` = use the registry (or derive it from the domain). |
| `RUN_ALL_PAYERS` | `True` = ignore `HOMEPAGE_URL` and crawl every payer in the registry (up to `PARALLEL_PAYERS` at once). |
| `EXTRA_START_URLS` | Extra entry-point URLs (e.g. a direct link to a policy hub) for the payer in `HOMEPAGE_URL`. |
| `RESET_PAYER_STATE` | `True` = forget the checkpoint for this payer and start over. |

**Adding or changing a payer:** add a dict to `PAYER_REGISTRY` in the same file:
```python
{"payer_name": "Example Health", "payer_alias": "Example Health Plan", "hint_host": "examplehealth.com",
 "footprint_state": "OH",                       # only for single-state payers, else ""
 "affiliated_domains": ["examplehealth.com", "examplerx.com"],   # domains the payer owns (crawled as in-scope)
 "brand_terms": ["Example Health", "ExampleRx"]},               # names that prove a document on a CDN/foreign host is theirs
```
Alternatively place a `payer_seed_list.csv` next to the script (this file is optional and not in the repo). It needs a
header row with a payer-name column (`payer`, `payer_name` or `name`) and a host column (`hint_host`, `host`, `homepage` or
`url`); it renames/extends the registry by host.

### 4.2 Section 2 – SETTINGS (how to crawl)

| Setting(s) | What it controls |
|---|---|
| `CONTACT`, `USER_AGENT` | **Put a real contact e-mail in `CONTACT`.** It is sent in the User-Agent and a `From` header of HTTP requests (the headless browser sends only the `From` header) so site owners can reach you. |
| `REQUEST_DELAY_SECONDS`, `MAX_CRAWL_DELAY_SECONDS` | Minimum gap between requests to one host; cap applied to a robots.txt `Crawl-delay`. The crawler obeys robots.txt. |
| `REQUEST_TIMEOUT_SECONDS`, `MAX_RETRIES`, `RETRY_STATUSES` | HTTP timeout and retry behaviour. |
| `MAX_PAGES_PER_PAYER`, `MAX_DOCUMENTS_PER_PAYER`, `MAX_DEPTH` | Crawl size limits and link depth from the seed. Raise them for deeper coverage, lower them for quick test runs. |
| `MAX_SITEMAP_URLS`, `MAX_SITEMAP_FILES`, `KNOWN_PATTERN_MIN_DOCS` | Sitemap limits; the known-URL-pattern fallback runs if fewer documents than this were found. |
| `DOC_CANDIDATE_MIN_SCORE`, `MAYBE_DOC_MIN_SCORE`, `EXTERNAL_DOC_MIN_SCORE`, `PAGE_FOLLOW_MIN_SCORE`, `NAV_FOLLOW_MIN_SCORE` | Link-score thresholds that decide which documents are downloaded and which pages are followed. Lower = more recall and more noise, higher = stricter. |
| `RELATED_DOMAIN_*`, `RELATED_TRIAL_*` | How readily the crawler opens non-affiliated "related" domains and when it drops them. |
| `REVISION_POLICY` | `"latest_only"` keeps only the latest dated revision of a policy (older ones go to `skipped_doc_links.csv`); `"flag"` keeps all and flags superseded ones in `notes`. |
| `INFER_STATE_FROM_PAYER_FOOTPRINT` | Use `footprint_state` of single-state payers when no state is found in the document. |
| `CDN_HOST_SUFFIXES`, `CDN_TRUST_MIN_DOCS`, `IDP_HOST_SUFFIXES` | Shared file hosts (CDNs) needing proof of ownership; login-provider hosts used to detect login walls. |
| `PARALLEL_PAYERS`, `FETCH_WORKERS`, `PREFETCH_AHEAD` | Speed: payers crawled at once (only with `RUN_ALL_PAYERS`), background fetch threads, documents prefetched. |
| `USE_PLAYWRIGHT`, `PLAYWRIGHT_NAV_TIMEOUT_MS`, `MAX_RENDERS_PER_PAYER`, `MAX_EXPAND_CLICKS`, `MAX_TAB_CLICKS`, `MAX_PAGINATION_STEPS` | Headless browser on/off, timeout and the limits for rendering, accordion/tab clicking and pagination. |
| `USE_LLM`, `OLLAMA_URL`, `OLLAMA_MODEL`, `MAX_LLM_CALLS_PER_PAYER`, `LLM_FOR_MISSING_FIELDS` | LLM on/off, server address, model name (must be pulled in Ollama) and call budget per payer. |
| `USE_OCR` | OCR for scanned PDFs on/off (needs Tesseract). |
| `SAVE_DOCUMENT_FILES` | `True` = also keep a copy of every accepted document under `output/files/`. |
| `PROGRESS_BAR`, `CONSOLE_LOG_LEVEL` | Terminal UI and console verbosity (`logging.INFO` / `logging.DEBUG`). |
| `OUTPUT_DIR`, `STATE_DB`, `LOG_DIR` | Where results, checkpoint and logs go (default: `output/` next to the script). |

### 4.3 Section 3 – SCHEMA

`COLUMNS` and `ENUMS` define the 22 output columns and their allowed values; they are checked against
`schema_dictionary.xlsx` at start-up and the run aborts if the column names differ. `EXCLUDED_DOC_TYPES` lists the document
types that are not collected (default `provider_manual`, `bulletin`) – remove an entry to collect that type.

### 4.4 Keyword scoring (`crawler_text.py`)

What counts as a "policy-like" link or document is defined by the keyword/weight lists `POSITIVE_TERMS`, `NAV_TERMS` and
`NEGATIVE_TERMS` and the document-type rules in `crawler_text.py`. Edit these to tune what the crawler considers relevant.

## 5. Project layout

| File | Role |
|---|---|
| `payer_policy_crawler.py` | entry point; crawl orchestration (`PayerCrawler`), export, `main()` |
| `crawler_config.py` | **configuration**, schema constants, logging, progress UI |
| `crawler_text.py` | URL/text helpers, keyword scoring, document classification, metadata extraction |
| `crawler_io.py` | HTTP fetcher, robots.txt handling, Playwright wrapper, PDF/HTML/Office inspection, OCR |
| `crawler_state.py` | SQLite state/checkpoint, LLM client, output row model and schema validation |
| `schema_dictionary.xlsx` | the 22-column schema definition |
| `CRAWL_LOGIC_REVIEW.md` | review notes on known gaps in the crawl logic |

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `browser_unavailable` in the log | Run `python -m playwright install --with-deps chromium` inside the activated venv. |
| Chromium fails to start on Linux with missing `.so` libraries | Run `sudo python -m playwright install-deps chromium` (use the venv's python path with sudo). |
| `llm_unavailable` | Ollama is not running: start it (`ollama serve`, or open the app on macOS/Windows). |
| `llm_model_missing` | Run `ollama pull qwen2.5:3b`, or change `OLLAMA_MODEL` to a model shown by `ollama list`. |
| "scanned PDF … OCR unavailable" in `notes` | Tesseract is not installed or not on `PATH` (on Windows add `C:\Program Files\Tesseract-OCR` to `PATH` and open a new terminal), or `pytesseract`/`Pillow` is not installed. |
| `ModuleNotFoundError` | The venv is not activated, or `pip install -r requirements.txt` was not run in it. |
| Many pages "blocked" | The site uses bot protection or login; see `output/blocked_pages.csv`. Only public documents are collected. |
| Want a clean re-run | Set `RESET_PAYER_STATE = True` for one run, or delete `output/crawl_state_v2.db`. |
