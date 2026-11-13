"""Phase 4: the whole flow in one call - (crawler) -> collect -> compare -> verify -> Excel files."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .collector import CollectReport, collect_archive, collect_searxng
from .compare import compare, load_crawler_view
from .excel_export import export as export_links
from .gap_export import export as export_gap
from .store import LinkStore
from .verify import VerifyStore, verify_links

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_crawler(payer, runner=subprocess.run) -> int:
    """Runs the crawler unchanged, as its own process, for one payer."""
    cmd = [sys.executable, "payer_policy_crawler.py", "--homepage", payer.hint_host]
    print(f"== crawler: {' '.join(cmd[1:])}")
    rc = runner(cmd, cwd=str(PROJECT_ROOT)).returncode
    if rc:
        print(f"   crawler ended with exit code {rc}; continuing with what it saved")
    return rc


def collect(payer, source: str, limit: int, max_queries: int, **kw) -> CollectReport:
    print(f"== {payer.name} ({len(payer.domains)} domain(s))")
    store, rep = LinkStore(payer.name), CollectReport(payer.name)
    if source in ("all", "archive"):
        print(" archive indexes:")
        collect_archive(payer, store, rep, limit=limit, **kw)
    if source in ("all", "searxng"):
        print(f" SearXNG (next {max_queries} queries):")
        collect_searxng(payer, store, rep, max_queries=max_queries)
    store.save()
    if source in ("all", "archive"):
        print(f" archive: {rep.archive_seen} candidates, {rep.archive_new} new links"
              + (f", {len(rep.archive_errors)} skipped pass(es)" if rep.archive_errors else ""))
        for e in rep.archive_errors[:5]:
            print(f"   skipped: {e}")
    if source in ("all", "searxng"):
        print(f" SearXNG: {rep.searxng_queries_run} queries run, {rep.searxng_new} new links, "
              f"{rep.searxng_queries_left} left for later runs"
              + (f"; stopped early: {rep.searxng_stopped}" if rep.searxng_stopped else ""))
    print(f" total links stored for {payer.name}: {len(store.rows)}")
    return rep


def verify(payer, crawler_output=None, max_links=None) -> int:
    links = list(LinkStore(payer.name).rows.values())
    if not links:
        print(f"{payer.name}: no search links collected yet")
        return 0
    c = compare(payer.name, links, load_crawler_view(payer.name, crawler_output))
    store = VerifyStore(payer.name)
    left = sum(1 for r in c.search_only if r["url_key"] not in store.rows)
    print(f"== {payer.name}: {len(c.search_only)} search-only link(s), {left} not yet verified")
    n = verify_links(c.search_only, payer.alias or payer.name, store, max_links=max_links)
    print(f"   checked {n} in this run")
    return n


def write_reports(payers, crawler_output=None):
    comps, verified = [], {}
    for payer in payers:
        links = list(LinkStore(payer.name).rows.values())
        if not links:
            continue
        c = compare(payer.name, links, load_crawler_view(payer.name, crawler_output))
        comps.append(c)
        verified[payer.name] = VerifyStore(payer.name).rows
        note = "" if c.view.available else "  (no crawler output for this payer yet)"
        print(f"{payer.name}: {len(links)} search links | crawler documents {len(c.view.accepted)} | both "
              f"{len(c.found_by_both)} ({c.overlap_pct}% of crawler docs) | seen by crawler {len(c.seen_by_crawler)} "
              f"| search only {len(c.search_only)}{note}")
    links_xlsx = export_links()
    gap, final = export_gap(comps, verified)
    print(f"Excel: {links_xlsx}\nExcel: {gap}\nExcel: {final}")
    return links_xlsx, gap, final


def run(payers, crawl=False, searxng=False, skip_collect=False, limit=5000, max_queries=15, verify_max=200,
        crawler_output=None, runner=subprocess.run):
    for payer in payers:
        if crawl:
            run_crawler(payer, runner)
        if not skip_collect:
            collect(payer, "all" if searxng else "archive", limit, max_queries)
        if verify_max != 0:
            verify(payer, crawler_output, None if verify_max < 0 else verify_max)
    return write_reports(payers, crawler_output)
