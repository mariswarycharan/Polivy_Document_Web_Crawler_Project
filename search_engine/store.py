"""Per-payer link store (JSON lines in search_engine/output/). One record per url_key; a link found by
several sources is kept once with all its sources listed. Re-runs add to the store, so SearXNG batches
and archive passes accumulate."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import OUTPUT_DIR

FIELDS = ["payer", "url", "url_key", "sources", "kind", "mime", "title", "snippet", "query", "engine",
          "capture_date", "found_at"]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "payer"


def url_key(url: str) -> str:
    from crawler_text import url_key as crawler_url_key   # same normalisation as the crawler
    return crawler_url_key(url)


class LinkStore:
    def __init__(self, payer: str, out_dir: Path = OUTPUT_DIR) -> None:
        self.payer = payer
        self.path = Path(out_dir) / f"links_{slug(payer)}.jsonl"
        self.rows: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.rows[r["url_key"]] = r

    def add(self, url: str, source: str, **fields) -> bool:
        """Add or merge one link. Returns True when the url_key is new."""
        key = url_key(url)
        row = self.rows.get(key)
        if row is None:
            row = {f: "" for f in FIELDS}
            row.update(payer=self.payer, url=url, url_key=key, sources=[], found_at=now_utc())
            self.rows[key] = row
            new = True
        else:
            new = False
        if source not in row["sources"]:
            row["sources"].append(source)
        for k, v in fields.items():
            if v and not row.get(k):
                row[k] = v
        return new

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for r in self.rows.values():
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(self.path)


def load_all(out_dir: Path = OUTPUT_DIR) -> list[dict]:
    rows = []
    for p in sorted(Path(out_dir).glob("links_*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows
