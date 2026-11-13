"""Command line for the search-engine module. Run from the repository root:

    python -m search_engine check                     # is SearXNG up, and are the archive indexes reachable?
    python -m search_engine queries --payer Centene   # show the keyword queries built for a payer
    python -m search_engine sample --payer Centene    # one SearXNG query + a short archive listing
    python -m search_engine collect --payer Centene   # Phase 2: archive indexes + next SearXNG batch -> search_links.xlsx
    python -m search_engine collect --all             # every payer in the crawler's registry
    python -m search_engine export                    # rebuild search_links.xlsx from what is already collected
    python -m search_engine compare --all             # Phase 3: compare with the crawler output (no network)
    python -m search_engine verify --payer Centene    # Phase 3: check the search-only links, then re-run compare
    python -m search_engine run --payer Centene       # Phase 4: collect, compare, verify and write Excel in one go
"""
from __future__ import annotations

import argparse
import itertools
import sys
import threading
import logging
import warnings

warnings.filterwarnings("ignore", message=r"urllib3 .* doesn't match a supported version")  # harmless requests notice
logging.getLogger("cdx_toolkit").setLevel(logging.ERROR)   # its retry messages print whole HTML error pages

from . import archive_client
from .config import ARCHIVE_LIMIT_PER_DOMAIN, SEARXNG_BATCH_QUERIES
from .payers import find_payer, load_payers
from .query_builder import build_queries
from .searxng_client import SearxClient


def cmd_check(args) -> int:
    rc = 0
    print("SearXNG:")
    sx = SearxClient(delay=0)
    h = sx.health()
    sx.close()
    if h["error"]:
        print(f"  NOT OK - {h['error']}")
        rc = 1
    elif h["unresponsive"] or not h["results"]:
        print(f"  NOT OK - SearXNG is running at {h['url']}, but the search engines returned {h['results']} results")
        for u in h["unresponsive"]:
            print(f"    engine not answering: {u}")
        print("    (after a CAPTCHA, SearXNG pauses that engine for 24 h by default)")
        rc = 1
    else:
        print(f"  OK - {h['url']} answered, {h['results']} results from engines {h['engines']}")
    print("Archive indexes:")
    domain = args.domain or load_payers()[0].hint_host
    for src in ("cc", "ia"):
        name = archive_client.SOURCE_NAMES[src]
        ok, value = _with_time_limit(
            lambda: list(itertools.islice(archive_client.list_domain(domain, src, limit=50), 5)), ARCHIVE_CHECK_SECONDS)
        if ok:
            print(f"  OK - {name}: {len(value)} candidate URL(s) seen for {domain} in a 50-capture sample")
        else:
            print(f"  NOT OK - {name}: {value}")
            rc = 1
    return rc


ARCHIVE_CHECK_SECONDS = int(__import__("os").environ.get("SE_ARCHIVE_CHECK_SECONDS", "90"))


def _with_time_limit(fn, seconds):
    """Run fn in a background thread; give up after `seconds` (the archive servers can be slow to answer)."""
    box = {}
    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # network problems, index not reachable
            box["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return False, f"no answer within {seconds} s (server busy or network blocked)"
    if "error" in box:
        return False, box["error"]
    return True, box["value"]


def cmd_queries(args) -> int:
    payer = find_payer(load_payers(), args.payer)
    qs = build_queries(payer)
    print(f"{payer.name}: {len(payer.domains)} domain(s), {len(qs)} queries")
    for q in qs[: args.show]:
        print("  ", q.text)
    if len(qs) > args.show:
        print(f"   ... and {len(qs) - args.show} more")
    return 0


def cmd_sample(args) -> int:
    payer = find_payer(load_payers(), args.payer)
    q = build_queries(payer, domains=[payer.hint_host])[0]
    print(f"SearXNG query: {q.text}")
    sx = SearxClient()
    resp = sx.search(q.text, max_pages=1)
    sx.close()
    if resp.error:
        print(f"  error: {resp.error}")
    for r in resp.results[:10]:
        print(f"  [{r.engine}] {r.url}")
    print(f"Archive sample for {payer.hint_host} (Common Crawl, first 10 candidates):")
    try:
        for hit in itertools.islice(archive_client.list_domain(payer.hint_host, "cc", limit=2000), 10):
            print(f"  {hit.timestamp} {hit.mime:<20} {hit.url}")
    except Exception as e:
        print(f"  error: {type(e).__name__}: {str(e)[:150]}")
    return 0


def _archives(args):
    return {"both": None, "commoncrawl": ["cc"], "wayback": ["ia"]}[args.archives]


def cmd_collect(args) -> int:
    from .excel_export import export
    from .pipeline import collect
    payers = load_payers() if args.all else [find_payer(load_payers(), args.payer)]
    for payer in payers:
        collect(payer, args.source, args.limit, args.max_queries, refresh=args.refresh, archives=_archives(args))
    print(f"Excel: {export()}")
    return 0


def cmd_export(args) -> int:
    from .excel_export import export
    print(f"Excel: {export()}")
    return 0


def _payers_with_links(args):
    from .store import LinkStore
    payers = load_payers() if args.all else [find_payer(load_payers(), args.payer)]
    return [(p, list(LinkStore(p.name).rows.values())) for p in payers]


def _write_gap(pairs, crawler_output=None) -> None:
    from .compare import compare, load_crawler_view
    from .gap_export import export
    from .verify import VerifyStore
    comps, verified = [], {}
    for payer, links in pairs:
        if not links:
            continue
        c = compare(payer.name, links, load_crawler_view(payer.name, crawler_output))
        comps.append(c)
        verified[payer.name] = VerifyStore(payer.name).rows
        crawler_note = "" if c.view.available else "  (no crawler output for this payer yet)"
        print(f"{payer.name}: {len(links)} search links | crawler documents {len(c.view.accepted)} | "
              f"both {len(c.found_by_both)} ({c.overlap_pct}% of crawler docs) | seen by crawler "
              f"{len(c.seen_by_crawler)} | search only {len(c.search_only)}{crawler_note}")
    gap, final = export(comps, verified)
    print(f"Excel: {gap}\nExcel: {final}")


def cmd_compare(args) -> int:
    _write_gap(_payers_with_links(args), args.crawler_output)
    return 0


def cmd_verify(args) -> int:
    from .compare import compare, load_crawler_view
    from .verify import VerifyStore, verify_links
    pairs = _payers_with_links(args)
    for payer, links in pairs:
        if not links:
            print(f"{payer.name}: no search links collected yet")
            continue
        c = compare(payer.name, links, load_crawler_view(payer.name, args.crawler_output))
        store = VerifyStore(payer.name)
        left = sum(1 for r in c.search_only if r["url_key"] not in store.rows)
        print(f"== {payer.name}: {len(c.search_only)} search-only link(s), {left} not yet verified")
        n = verify_links(c.search_only, payer.alias or payer.name, store, max_links=args.max)
        print(f"   checked {n} in this run")
    _write_gap(pairs, args.crawler_output)
    return 0


def cmd_run(args) -> int:
    from .pipeline import run
    payers = load_payers() if args.all else [find_payer(load_payers(), args.payer)]
    run(payers, crawl=args.crawl, searxng=args.searxng, skip_collect=args.skip_collect, limit=args.limit,
        max_queries=args.max_queries, verify_max=args.verify_max, crawler_output=args.crawler_output,
        refresh=args.refresh, archives=_archives(args))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m search_engine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="health check of SearXNG and the archive indexes")
    c.add_argument("--domain", help="domain used for the archive check (default: first payer)")
    q = sub.add_parser("queries", help="list the keyword queries for one payer")
    q.add_argument("--payer", required=True, help="payer name or domain, e.g. Centene or centene.com")
    q.add_argument("--show", type=int, default=20)
    s = sub.add_parser("sample", help="run one query and a short archive listing for one payer")
    s.add_argument("--payer", required=True)
    co = sub.add_parser("collect", help="Phase 2: collect links into search_engine/output/search_links.xlsx")
    g = co.add_mutually_exclusive_group(required=True)
    g.add_argument("--payer", help="payer name or domain")
    g.add_argument("--all", action="store_true", help="every payer in the crawler's registry")
    co.add_argument("--source", choices=["all", "archive", "searxng"], default="all")
    co.add_argument("--refresh", action="store_true", help="re-read archive passes finished in earlier runs")
    co.add_argument("--archives", choices=["both", "commoncrawl", "wayback"], default="both",
                    help="which archive index to read (default both)")
    co.add_argument("--limit", type=int, default=ARCHIVE_LIMIT_PER_DOMAIN,
                    help="archive captures read per domain, source and pass")
    co.add_argument("--max-queries", type=int, default=SEARXNG_BATCH_QUERIES,
                    help="SearXNG queries in this run (the next run continues)")
    sub.add_parser("export", help="rebuild search_links.xlsx from the stored links")
    for name, helptext in (("compare", "Phase 3: compare search links with the crawler output (no network)"),
                           ("verify", "Phase 3: fetch and check the search-only links, then write the gap files")):
        p = sub.add_parser(name, help=helptext)
        g2 = p.add_mutually_exclusive_group(required=True)
        g2.add_argument("--payer")
        g2.add_argument("--all", action="store_true")
        p.add_argument("--crawler-output", help="crawler output folder to compare with (default: this project's output/)")
        if name == "verify":
            p.add_argument("--max", type=int, default=None, help="check at most this many links per payer in this run")
    rn = sub.add_parser("run", help="Phase 4: collect, compare, verify and write the Excel files in one go")
    g3 = rn.add_mutually_exclusive_group(required=True)
    g3.add_argument("--payer")
    g3.add_argument("--all", action="store_true")
    rn.add_argument("--crawl", action="store_true", help="run the crawler for the payer first (unchanged, own process)")
    rn.add_argument("--searxng", action="store_true", help="also run the next SearXNG batch")
    rn.add_argument("--skip-collect", action="store_true", help="use the links already collected")
    rn.add_argument("--refresh", action="store_true", help="re-read archive passes finished in earlier runs")
    rn.add_argument("--archives", choices=["both", "commoncrawl", "wayback"], default="both",
                    help="which archive index to read (default both)")
    rn.add_argument("--limit", type=int, default=ARCHIVE_LIMIT_PER_DOMAIN)
    rn.add_argument("--max-queries", type=int, default=SEARXNG_BATCH_QUERIES)
    rn.add_argument("--verify-max", type=int, default=200, help="links checked per payer (0 = none, -1 = all)")
    rn.add_argument("--crawler-output", help="crawler output folder to compare with (default: this project's output/)")
    args = ap.parse_args(argv)
    return {"run": cmd_run, "check": cmd_check, "queries": cmd_queries, "sample": cmd_sample, "collect": cmd_collect,
            "export": cmd_export, "compare": cmd_compare, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
