"""Lists document URLs on a payer's domains from the public web-archive indexes
(Common Crawl and the Internet Archive), using cdx_toolkit.

Two passes per domain, both filtered on the index server so little data is transferred:
  docs  - status 200 captures of PDF / Word / Excel files
  pages - status 200 HTML captures whose URL looks policy-related
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from .config import (ARCHIVE_FROM, ARCHIVE_LIMIT_PER_DOMAIN, DOCUMENT_EXTENSIONS, DOCUMENT_MIME_TYPES,
                     POLICY_PATH_HINTS)

SOURCE_NAMES = {"cc": "commoncrawl", "ia": "wayback"}
MAX_SERVER_ERRORS = 3     # consecutive 5xx answers to the same index request before that domain is skipped


class ArchiveUnavailable(Exception):
    """The archive index server kept failing for one request; the caller skips that domain and moves on."""


class _GiveUpRequests:
    """Stands in for the `requests` module inside cdx_toolkit. cdx_toolkit retries 5xx answers forever;
    this gives up after MAX_SERVER_ERRORS consecutive failures of the same request."""

    def __init__(self, real):
        self._real = real
        self._fails: dict[str, int] = {}

    def __getattr__(self, name):          # exceptions, Session, ... come from the real module
        return getattr(self._real, name)

    def get(self, url, params=None, **kw):
        resp = self._real.get(url, params=params, **kw)
        key = f"{url} {params!r}"
        if resp.status_code in (429, 500, 502, 503, 504, 509):
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            if n >= MAX_SERVER_ERRORS:
                self._fails.pop(key, None)
                raise ArchiveUnavailable(f"index server answered HTTP {resp.status_code} {n} times in a row")
        else:
            self._fails.pop(key, None)
        return resp


def _install_give_up() -> None:
    import cdx_toolkit.myrequests as m
    if not isinstance(m.requests, _GiveUpRequests):
        m.requests = _GiveUpRequests(m.requests)
DOC_MIME_REGEX = r"application/(pdf|msword|vnd\.openxmlformats-officedocument\..*|vnd\.ms-excel)"


@dataclass
class ArchiveHit:
    url: str
    mime: str
    status: str
    timestamp: str     # capture time, YYYYMMDDhhmmss
    source: str        # commoncrawl / wayback
    kind: str          # docs / pages


def is_candidate(url: str, mime: str) -> bool:
    """Keep document files, and web pages whose path looks policy-related."""
    path = urlsplit(url).path.lower()
    mime = (mime or "").lower().split(";")[0].strip()
    if mime in DOCUMENT_MIME_TYPES or path.endswith(DOCUMENT_EXTENSIONS):
        return True
    return mime in ("text/html", "application/xhtml+xml", "") and any(h in path for h in POLICY_PATH_HINTS)


def server_filters(source: str, kind: str) -> list[str]:
    """CDX filters. Common Crawl (pywb) needs '=' for exact and '~' for regex; the Internet Archive
    accepts no modifiers and treats every filter as a regex."""
    hints = "|".join(re.escape(h) for h in POLICY_PATH_HINTS)
    if source == "cc":
        base = ["=status:200"]
        return base + [f"~mime:{DOC_MIME_REGEX}"] if kind == "docs" else \
            base + ["=mime:text/html", f"~url:(?i).*({hints}).*"]
    base = ["status:200"]
    return base + [f"mime:{DOC_MIME_REGEX}"] if kind == "docs" else \
        base + ["mime:text/html", f"url:(?i).*({hints}).*"]


def list_domain(domain: str, source: str = "cc", limit: int = ARCHIVE_LIMIT_PER_DOMAIN, fetcher=None,
                kinds: tuple[str, ...] = ("docs", "pages")) -> Iterator[ArchiveHit]:
    """Yield candidate captures for a domain and all its subdomains."""
    if fetcher is None:
        import cdx_toolkit
        _install_give_up()
        fetcher = cdx_toolkit.CDXFetcher(source=source)
    extra = {"from_ts": ARCHIVE_FROM} if ARCHIVE_FROM else {}
    for kind in kinds:
        for obj in fetcher.iter(f"*.{domain}", limit=limit, filter=server_filters(source, kind), **extra):
            url = obj.get("url", "")
            mime = obj.get("mime") or obj.get("mimetype") or ""
            if url and is_candidate(url, mime):
                yield ArchiveHit(url=url, mime=mime, status=str(obj.get("status", "")),
                                 timestamp=str(obj.get("timestamp", "")), source=SOURCE_NAMES.get(source, source),
                                 kind=kind)
