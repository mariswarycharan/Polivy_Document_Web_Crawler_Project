"""Payer list for the search engine, read (never modified) from the crawler's PAYER_REGISTRY."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Payer:
    name: str
    hint_host: str
    domains: tuple[str, ...]                 # hint host first, then affiliated domains, no duplicates
    brand_terms: tuple[str, ...] = field(default=())


def _clean_domain(d: str) -> str:
    d = (d or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0]
    return d[4:] if d.startswith("www.") else d


def from_registry(registry: list[dict]) -> list[Payer]:
    out = []
    for p in registry:
        hint = _clean_domain(p.get("hint_host", ""))
        domains = [hint] + [_clean_domain(d) for d in p.get("affiliated_domains", [])]
        seen, ordered = set(), []
        for d in domains:
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
        out.append(Payer(name=p["payer_name"], hint_host=hint, domains=tuple(ordered),
                         brand_terms=tuple(p.get("brand_terms", []))))
    return out


def load_payers() -> list[Payer]:
    from crawler_config import PAYER_REGISTRY  # read-only use of the crawler's registry
    return from_registry(PAYER_REGISTRY)


def find_payer(payers: list[Payer], key: str) -> Payer:
    """Match by payer name (case-insensitive, partial) or by any of its domains."""
    k = key.strip().lower()
    dom = _clean_domain(k)
    for p in payers:
        if p.name.lower() == k or dom in p.domains:
            return p
    matches = [p for p in payers if k in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(p.name for p in payers)
    raise SystemExit(f"Payer '{key}' not found or ambiguous. Known payers: {names}")
