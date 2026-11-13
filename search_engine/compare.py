"""Phase 3a: compares the search engine's links with everything the crawler has seen.

Groups for every search link of a payer:
  found_by_both   - the crawler also has it in output.csv
  seen_by_crawler - the crawler queued it but did not output it (rejected, failed, blocked, pending ...)
  search_only     - the crawler never saw it: the candidates to verify
"""
from __future__ import annotations

import csv
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .store import url_key


def crawler_output_dir() -> Path:
    try:
        from crawler_config import OUTPUT_DIR
        return Path(OUTPUT_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent / "output"


@dataclass
class CrawlerView:
    payer: str
    accepted: dict[str, dict] = field(default_factory=dict)      # url_key -> output.csv row
    seen: dict[str, str] = field(default_factory=dict)           # url_key -> "status: reason"
    crawled_hosts: set[str] = field(default_factory=set)         # hosts with at least one page crawled
    pending: int = 0                                             # items still queued (crawl not finished)
    available: bool = False                                      # crawler data found for this payer


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _open_db(path: Path) -> sqlite3.Connection:
    """Read the crawler's checkpoint without touching it (a copy if it is locked by a running crawl)."""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.execute("select 1 from frontier limit 1")
        return con
    except sqlite3.Error:
        tmp = Path(tempfile.mkdtemp()) / "crawl_state_copy.db"
        shutil.copy2(path, tmp)
        for ext in ("-wal", "-shm"):
            if Path(str(path) + ext).exists():
                shutil.copy2(str(path) + ext, str(tmp) + ext)
        return sqlite3.connect(str(tmp))


def load_crawler_view(payer: str, out_dir: Path | None = None) -> CrawlerView:
    out_dir = Path(out_dir or crawler_output_dir())
    view = CrawlerView(payer)
    out_csv = out_dir / "output.csv"
    if out_csv.exists():
        with out_csv.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("payer_name") == payer and r.get("document_url"):
                    view.accepted[url_key(r["document_url"])] = r
    db_path = out_dir / "crawl_state_v2.db"
    if db_path.exists():
        con = _open_db(db_path)
        try:
            for url, kind, status, reason in con.execute(
                    "SELECT url, kind, status, reason FROM frontier WHERE payer=?", (payer,)):
                view.seen[url_key(url)] = f"{status}: {reason or ''}".strip(": ")
                if kind == "page" and status == "done":
                    view.crawled_hosts.add(_host(url))
                if status in ("pending", "in_progress"):
                    view.pending += 1
            for url, reason in con.execute("SELECT url, reason FROM skipped_links WHERE payer=?", (payer,)):
                view.seen.setdefault(url_key(url), f"skipped_low_score: {reason or ''}")
        finally:
            con.close()
    view.available = bool(view.accepted or view.seen)
    return view


def why_missed(url: str, view: CrawlerView) -> str:
    if not view.available:
        return "no crawler output for this payer yet"
    if _host(url) not in view.crawled_hosts:
        return "host never crawled"
    if view.pending:
        return f"not reached - crawl not finished ({view.pending} items still queued)"
    return "not linked from any page the crawler visited"


@dataclass
class Comparison:
    payer: str
    view: CrawlerView
    found_by_both: list[dict] = field(default_factory=list)
    seen_by_crawler: list[dict] = field(default_factory=list)
    search_only: list[dict] = field(default_factory=list)

    @property
    def overlap_pct(self) -> float:
        n = len(self.view.accepted)
        return round(100.0 * len(self.found_by_both) / n, 1) if n else 0.0

    def missed_reasons(self) -> Counter:
        return Counter(r["why_missed"] for r in self.search_only)


def compare(payer: str, links: list[dict], view: CrawlerView) -> Comparison:
    cmp = Comparison(payer, view)
    for r in links:
        k = r["url_key"]
        if k in view.accepted:
            cmp.found_by_both.append(r)
        elif k in view.seen:
            cmp.seen_by_crawler.append({**r, "crawler_status": view.seen[k]})
        else:
            cmp.search_only.append({**r, "why_missed": why_missed(r["url"], view)})
    return cmp
