"""Client for a local SearXNG instance (JSON API)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from .config import (SEARXNG_DELAY_SECONDS, SEARXNG_ENGINES, SEARXNG_MAX_PAGES, SEARXNG_TIMEOUT_SECONDS,
                     SEARXNG_URL, user_agent)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    engine: str
    page: int


@dataclass
class PageResponse:
    results: list[SearchResult] = field(default_factory=list)
    unresponsive: list[str] = field(default_factory=list)   # "engine: reason", e.g. suspended / CAPTCHA
    error: str = ""


class SearxClient:
    def __init__(self, base_url: str = SEARXNG_URL, engines: list[str] | None = None,
                 delay: float = SEARXNG_DELAY_SECONDS, timeout: float = SEARXNG_TIMEOUT_SECONDS,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.engines = engines or list(SEARXNG_ENGINES)
        self.delay = delay
        self._last = 0.0
        self.client = httpx.Client(timeout=timeout, transport=transport, trust_env=False,  # local service: never via a proxy
                                   headers={"User-Agent": user_agent(), "Accept": "application/json"})

    def close(self) -> None:
        self.client.close()

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def search_page(self, query: str, page: int = 1) -> PageResponse:
        self._wait()
        params = {"q": query, "format": "json", "pageno": page, "engines": ",".join(self.engines),
                  "safesearch": 0}
        try:
            r = self.client.get(f"{self.base_url}/search", params=params)
        except httpx.HTTPError as e:
            return PageResponse(error=f"cannot reach SearXNG at {self.base_url}: {type(e).__name__}")
        if r.status_code == 403:
            return PageResponse(error="SearXNG refused JSON (403): add 'json' to search.formats in settings.yml")
        if r.status_code == 429:
            return PageResponse(error="SearXNG limiter blocked the request (429): set server.limiter to false")
        if r.status_code != 200:
            return PageResponse(error=f"SearXNG returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            return PageResponse(error="SearXNG did not return JSON")
        results = [SearchResult(url=x.get("url", ""), title=x.get("title", "") or "",
                                snippet=x.get("content", "") or "", engine=x.get("engine", "") or "", page=page)
                   for x in data.get("results", []) if x.get("url")]
        unresponsive = [f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) > 1 else str(e)
                        for e in data.get("unresponsive_engines", [])]
        return PageResponse(results=results, unresponsive=unresponsive)

    def search(self, query: str, max_pages: int = SEARXNG_MAX_PAGES) -> PageResponse:
        """All result pages for one query; stops early on an empty page, a repeat page or an error."""
        total = PageResponse()
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            resp = self.search_page(query, page)
            total.unresponsive.extend(u for u in resp.unresponsive if u not in total.unresponsive)
            if resp.error:
                total.error = resp.error
                break
            new = [r for r in resp.results if r.url not in seen]
            if not new:
                break
            seen.update(r.url for r in new)
            total.results.extend(new)
        return total

    def health(self) -> dict:
        """One small test query: is SearXNG up, is JSON enabled, which engines answer."""
        resp = self.search_page("medical policy", 1)
        return {"url": self.base_url, "ok": not resp.error and not resp.unresponsive,
                "results": len(resp.results), "error": resp.error, "unresponsive": resp.unresponsive,
                "engines": self.engines}
