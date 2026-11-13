"""Phase 2: collects links for a payer from the archive indexes and from SearXNG into the link store."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import archive_client
from .config import DOCUMENT_EXTENSIONS, ARCHIVE_LIMIT_PER_DOMAIN, ARCHIVE_SOURCES, OUTPUT_DIR, SEARXNG_BATCH_QUERIES, SEARXNG_MAX_PAGES
from .payers import Payer
from .query_builder import build_queries
from .searxng_client import SearxClient
from urllib.parse import urlsplit

from .store import LinkStore, now_utc, slug


@dataclass
class CollectReport:
    payer: str
    archive_new: int = 0
    archive_seen: int = 0
    archive_errors: list[str] = field(default_factory=list)
    archive_resumed: int = 0           # passes skipped because an earlier run finished them
    searxng_queries_run: int = 0
    searxng_queries_left: int = 0
    searxng_new: int = 0
    searxng_seen: int = 0
    searxng_stopped: str = ""          # why the batch stopped early (CAPTCHA, suspended, unreachable)


def archive_progress_path(payer: Payer, out_dir: Path = OUTPUT_DIR) -> Path:
    return Path(out_dir) / f"archive_progress_{slug(payer.name)}.json"


def collect_archive(payer: Payer, store: LinkStore, report: CollectReport, sources=ARCHIVE_SOURCES,
                    limit: int = ARCHIVE_LIMIT_PER_DOMAIN, fetcher_factory=None, log=print,
                    out_dir: Path | None = None, refresh: bool = False) -> None:
    """Passes that finished in an earlier run are skipped (so a stopped run resumes); skipped-with-error
    passes are tried again. refresh=True re-reads everything."""
    prog_path = archive_progress_path(payer, out_dir or store.path.parent)   # progress lives next to the links
    done: dict = {} if refresh or not prog_path.exists() else json.loads(prog_path.read_text(encoding="utf-8"))
    for src in sources:
        fetcher = fetcher_factory(src) if fetcher_factory else None
        name = archive_client.SOURCE_NAMES.get(src, src)
        for domain in payer.domains:
            for kind in ("docs", "pages"):
                pkey = f"{src}|{domain}|{kind}"
                if pkey in done and not done[pkey].get("error"):
                    report.archive_resumed += 1
                    continue
                n, note = 0, ""
                log(f"  {name:<11} {domain:<35} {kind:<5} searching ...")
                try:
                    for hit in archive_client.list_domain(domain, src, limit=limit, fetcher=fetcher, kinds=(kind,)):
                        n += 1
                        report.archive_seen += 1
                        if store.add(hit.url, hit.source, kind=hit.kind, mime=hit.mime,
                                     capture_date=hit.timestamp[:8]):
                            report.archive_new += 1
                except Exception as e:   # one domain/source/pass failing must not stop the rest
                    note = f"  SKIPPED - {type(e).__name__}: {str(e)[:100]}"
                    report.archive_errors.append(f"{name} {domain} {kind}: {type(e).__name__}: {str(e)[:120]}")
                log(f"  {name:<11} {domain:<35} {kind:<5} {n} candidate URL(s){note}")
                store.save()
                done[pkey] = {"candidates": n, "error": note.strip(), "at": now_utc()}
                prog_path.parent.mkdir(parents=True, exist_ok=True)
                prog_path.write_text(json.dumps(done, indent=1), encoding="utf-8")


def _progress_path(payer: Payer, out_dir: Path) -> Path:
    return Path(out_dir) / f"searxng_progress_{slug(payer.name)}.json"


def collect_searxng(payer: Payer, store: LinkStore, report: CollectReport, client: SearxClient | None = None,
                    max_queries: int = SEARXNG_BATCH_QUERIES, max_pages: int = SEARXNG_MAX_PAGES,
                    out_dir: Path = OUTPUT_DIR, log=print) -> None:
    """Run the next batch of not-yet-done queries. Stops at once when the engine shows a CAPTCHA,
    is suspended, or SearXNG cannot be reached; the next run continues from the same point."""
    prog_path = _progress_path(payer, out_dir)
    done: dict = json.loads(prog_path.read_text(encoding="utf-8")) if prog_path.exists() else {}
    todo = [q for q in build_queries(payer) if q.text not in done]
    own_client = client is None
    client = client or SearxClient()
    try:
        for q in todo[:max_queries]:
            resp = client.search(q.text, max_pages=max_pages)
            if resp.error or resp.unresponsive:
                report.searxng_stopped = resp.error or "; ".join(resp.unresponsive)
                log(f"  stopped: {report.searxng_stopped}")
                break
            report.searxng_queries_run += 1
            for r in resp.results:
                report.searxng_seen += 1
                kind = "docs" if urlsplit(r.url).path.lower().endswith(DOCUMENT_EXTENSIONS) else "pages"
                if store.add(r.url, "searxng", kind=kind, title=r.title, snippet=r.snippet[:500], query=q.text,
                             engine=r.engine):
                    report.searxng_new += 1
            done[q.text] = {"results": len(resp.results), "at": now_utc()}
            log(f"  {len(resp.results):>3} result(s)  {q.text}")
            store.save()
            prog_path.parent.mkdir(parents=True, exist_ok=True)
            prog_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
    finally:
        if own_client:
            client.close()
    report.searxng_queries_left = len([q for q in build_queries(payer) if q.text not in done])
