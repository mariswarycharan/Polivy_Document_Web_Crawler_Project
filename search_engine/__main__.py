"""Command line for the search-engine module. Run from the repository root:

    python -m search_engine check                     # is SearXNG up, and are the archive indexes reachable?
    python -m search_engine queries --payer Centene   # show the keyword queries built for a payer
    python -m search_engine sample --payer Centene    # one SearXNG query + a short archive listing
"""
from __future__ import annotations

import argparse
import itertools
import sys
import threading

from . import archive_client
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
    else:
        print(f"  OK - {h['url']} answered, {h['results']} results from engines {h['engines']}")
        for u in h["unresponsive"]:
            print(f"  warning - engine not answering: {u}")
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
    args = ap.parse_args(argv)
    return {"check": cmd_check, "queries": cmd_queries, "sample": cmd_sample}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
