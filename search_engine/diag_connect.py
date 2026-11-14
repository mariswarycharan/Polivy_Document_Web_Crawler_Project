"""Connection check: plain httpx vs the crawler's Fetcher client, printing the real error text.

    python -m search_engine.diag_connect
"""
from __future__ import annotations

import os
import socket
import sys

import httpx

URLS = ["https://www.anthem.com/robots.txt",
        "https://www.anthem.com/agent/co/f1/s0/t0/pw_ad079885.pdf",
        "http://www.anthem.com/agent/co/f3/s1/t0/pw_a032070.pdf"]


def show(label, fn):
    try:
        r = fn()
        print(f"  {label:<18} HTTP {r.status_code}  {r.headers.get('content-type', '')[:40]}")
    except Exception as e:
        cause = e.__cause__ or e.__context__
        print(f"  {label:<18} {type(e).__name__}: {e!s}"[:220])
        if cause:
            print(f"  {'':<18} cause: {type(cause).__name__}: {cause!s}"[:220])


def main() -> int:
    print("python:", sys.executable)
    print("proxy env:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()} or "none")
    try:
        print("DNS www.anthem.com:", sorted({a[4][0] for a in socket.getaddrinfo("www.anthem.com", 443)}))
    except Exception as e:
        print("DNS www.anthem.com: FAILED", e)
    from crawler_io import Fetcher
    f = Fetcher()
    plain = httpx.Client(follow_redirects=True, timeout=20)
    for u in URLS:
        print(u)
        show("plain httpx", lambda: plain.get(u))
        show("crawler client", lambda: f.client.get(u))
        show("no redirects", lambda: plain.get(u, follow_redirects=False))
        try:
            loc = plain.get(u, follow_redirects=False).headers.get("location")
            if loc:
                print(f"  {'':<18} redirects to: {loc[:150]}")
        except Exception:
            pass
    f.close()
    plain.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
