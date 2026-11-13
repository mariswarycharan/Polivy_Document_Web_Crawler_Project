"""Builds the keyword queries (payer domain x policy term) sent to SearXNG."""
from __future__ import annotations

from dataclasses import dataclass

from .config import FILE_TYPES, POLICY_TERMS
from .payers import Payer


@dataclass(frozen=True)
class Query:
    payer: str
    domain: str
    term: str
    file_type: str      # "" when no filetype restriction
    text: str           # the exact query string sent to the search engine


def build_queries(payer: Payer, terms: list[str] | None = None,
                  file_types: list[str] | None = None, domains: list[str] | None = None) -> list[Query]:
    terms = POLICY_TERMS if terms is None else terms
    file_types = FILE_TYPES if file_types is None else file_types
    out: list[Query] = []
    for domain in (domains or payer.domains):
        for term in terms:
            base = f'site:{domain} "{term}"'
            out.append(Query(payer.name, domain, term, "", base))
            for ft in file_types:
                out.append(Query(payer.name, domain, term, ft, f"{base} filetype:{ft}"))
    return out
