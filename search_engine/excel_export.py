"""Writes search_engine/output/search_links.xlsx from all per-payer link stores."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .config import OUTPUT_DIR
from .store import load_all

COLUMNS = ["payer", "url", "sources", "kind", "mime", "title", "snippet", "query", "engine", "capture_date",
           "found_at", "url_key"]
WIDTHS = {"payer": 18, "url": 70, "sources": 26, "kind": 8, "mime": 22, "title": 40, "snippet": 50,
          "query": 45, "engine": 12, "capture_date": 12, "found_at": 21, "url_key": 60}


def _clean(v) -> str:
    s = ", ".join(v) if isinstance(v, list) else str(v or "")
    return "".join(ch for ch in s if ch in "\t\n" or ord(ch) >= 32)[:32000]   # Excel rejects control characters


def export(out_dir: Path = OUTPUT_DIR) -> Path:
    rows = sorted(load_all(out_dir), key=lambda r: (r["payer"], r["url"]))
    wb = Workbook()
    ws = wb.active
    ws.title = "links"
    ws.append(COLUMNS)
    for r in rows:
        ws.append([_clean(r.get(c, "")) for c in COLUMNS])
    for i, c in enumerate(COLUMNS, 1):
        ws.cell(1, i).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(i)].width = WIDTHS[c]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    s = wb.create_sheet("summary")
    heads = ["payer", "links", "commoncrawl", "wayback", "searxng", "found by 2+ sources", "documents", "policy pages"]
    s.append(heads)
    by = defaultdict(list)
    for r in rows:
        by[r["payer"]].append(r)
    for payer, rs in by.items():
        src = Counter(x for r in rs for x in r["sources"])
        kind = Counter(r.get("kind") for r in rs)
        s.append([payer, len(rs), src["commoncrawl"], src["wayback"], src["searxng"],
                  sum(1 for r in rs if len(r["sources"]) > 1), kind["docs"], kind["pages"]])
    for i, h in enumerate(heads, 1):
        s.cell(1, i).font = Font(bold=True)
        s.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 2)

    out = Path(out_dir) / "search_links.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out
