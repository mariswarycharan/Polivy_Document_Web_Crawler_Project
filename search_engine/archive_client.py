"""Lists document URLs on a payer's domains from the public web-archive indexes
(Common Crawl and the Internet Archive), using cdx_toolkit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from .config import (ARCHIVE_LIMIT_PER_DOMAIN, DOCUMENT_EXTENSIONS, DOCUMENT_MIME_TYPES, POLICY_PATH_HINTS)

SOURCE_NAMES = {"cc": "commoncrawl", "ia": "wayback"}


@dataclass
class ArchiveHit:
    url: str
    mime: str
    status: str
    timestamp: str     # capture time, YYYYMMDDhhmmss
    source: str        # commoncrawl / wayback


def is_candidate(url: str, mime: str) -> bool:
    """Keep document files, and web pages whose path looks policy-related."""
    path = urlsplit(url).path.lower()
    mime = (mime or "").lower().split(";")[0].strip()
    if mime in DOCUMENT_MIME_TYPES or path.endswith(DOCUMENT_EXTENSIONS):
        return True
    return mime in ("text/html", "application/xhtml+xml", "") and any(h in path for h in POLICY_PATH_HINTS)


def list_domain(domain: str, source: str = "cc", limit: int = ARCHIVE_LIMIT_PER_DOMAIN,
                fetcher=None) -> Iterator[ArchiveHit]:
    """Yield candidate captures for a domain and all its subdomains (status 200 only)."""
    if fetcher is None:
        import cdx_toolkit
        fetcher = cdx_toolkit.CDXFetcher(source=source)
    status_filter = "=status:200" if source == "cc" else "status:200"
    for obj in fetcher.iter(f"*.{domain}", limit=limit, filter=[status_filter]):
        url = obj.get("url", "")
        mime = obj.get("mime") or obj.get("mimetype") or ""
        if url and is_candidate(url, mime):
            yield ArchiveHit(url=url, mime=mime, status=str(obj.get("status", "")),
                             timestamp=str(obj.get("timestamp", "")), source=SOURCE_NAMES.get(source, source))
