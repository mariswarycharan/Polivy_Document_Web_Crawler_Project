"""Phase 3c: writes the comparison and verification results.

  output/search_gap_analysis.xlsx  - summary per payer + every group in its own sheet
  output/search_engine_results.xlsx - the final result: verified policy documents the crawler did not find
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .compare import Comparison
from .config import OUTPUT_DIR
from .excel_export import _clean

RESULT_COLS = ["payer", "url", "title", "document_type", "file_type", "confidence", "content_score", "link_score",
               "sources", "why_missed", "reason", "crawler_verdict", "crawler_reason", "ai_verdict", "ai_confidence", "ai_reason",
               "ai_document_type", "ai_line_of_business", "http_status", "final_url", "checked_at", "url_key"]


def _sheet(wb, title, cols, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(cols)
    for r in rows:
        ws.append([_clean(r.get(c, "")) if not isinstance(r.get(c), (int, float)) else r.get(c) for c in cols])
    for i, c in enumerate(cols, 1):
        ws.cell(1, i).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(c, 70 if c in ("url", "final_url", "url_key") else 22)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _merge(row: dict, verdict: dict | None) -> dict:
    out = dict(row)
    if verdict:
        for k, v in verdict.items():
            if k not in ("url", "url_key") and (v or v == 0):
                out[k] = v
        out["verdict"] = verdict["verdict"]
    else:
        out["verdict"] = "not yet verified"
    return out


def export(comparisons: list[Comparison], verified: dict[str, dict[str, dict]], out_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    """verified: payer -> url_key -> verdict dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    summary = wb.active
    summary.title = "summary"
    heads = ["payer", "crawler documents", "search links", "found by both", "overlap % of crawler docs",
             "seen by crawler, not output", "search only", "verified: policy document", "verified: not policy",
             "verified: ambiguous", "unreachable", "not yet verified", "why missed (search only)"]
    summary.append(heads)
    groups = defaultdict(list)
    for c in comparisons:
        v = verified.get(c.payer, {})
        so = [_merge(r, v.get(r["url_key"])) for r in c.search_only]
        cnt = defaultdict(int)
        for r in so:
            cnt[r["verdict"]] += 1
        summary.append([c.payer, len(c.view.accepted), len(c.found_by_both) + len(c.seen_by_crawler) + len(c.search_only),
                        len(c.found_by_both), c.overlap_pct, len(c.seen_by_crawler), len(c.search_only),
                        cnt["accepted"], cnt["rejected"], cnt["ambiguous"], cnt["unreachable"], cnt["not yet verified"],
                        "; ".join(f"{k}: {n}" for k, n in c.missed_reasons().most_common())])
        groups["new_policy_documents"] += [r for r in so if r["verdict"] == "accepted"]
        groups["search_only_other"] += [r for r in so if r["verdict"] != "accepted"]
        groups["seen_by_crawler"] += c.seen_by_crawler
        groups["found_by_both"] += c.found_by_both
    for i, h in enumerate(heads, 1):
        summary.cell(1, i).font = Font(bold=True)
        summary.column_dimensions[get_column_letter(i)].width = 40 if i == len(heads) else max(12, min(len(h) + 2, 26))
    _sheet(wb, "new_policy_documents", RESULT_COLS, groups["new_policy_documents"])
    _sheet(wb, "search_only_other", ["payer", "verdict", "reason", "url", "title", "file_type", "content_score",
                                     "link_score", "http_status", "sources", "why_missed", "url_key"], groups["search_only_other"])
    _sheet(wb, "seen_by_crawler", ["payer", "url", "crawler_status", "sources", "kind", "title", "url_key"], groups["seen_by_crawler"])
    _sheet(wb, "found_by_both", ["payer", "url", "sources", "kind", "title", "url_key"], groups["found_by_both"])
    gap = out_dir / "search_gap_analysis.xlsx"
    wb.save(gap)

    res = Workbook()
    res.remove(res.active)
    by_rules = [r for r in groups["new_policy_documents"] if not r.get("ai_verdict")]
    by_ai = [r for r in groups["new_policy_documents"] if r.get("ai_verdict")]
    _sheet(res, "confirmed_by_rules", RESULT_COLS, by_rules)        # crawler rules accepted on their own
    _sheet(res, "accepted_by_local_AI", RESULT_COLS, by_ai)         # rules unsure, the local AI said yes - check these
    final = out_dir / "search_engine_results.xlsx"
    res.save(final)
    return gap, final
