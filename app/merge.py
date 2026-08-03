"""Merge results from multiple providers into a single list.

The same paper can show up from both OpenAlex and Google Scholar, so
duplicates need to be collapsed, and in the process we pick whichever
provider's data is better for each field (e.g. one provider might have the
abstract while another has the publisher).
"""

from __future__ import annotations

from .models import Paper, normalize_title

# Merge priority — the earlier provider's bibliographic data wins by default.
_PRIORITY = ("openalex", "gscholar")


def _rank(paper: Paper) -> int:
    for i, name in enumerate(_PRIORITY):
        if name in paper.sources:
            return i
    return len(_PRIORITY)


def _alias_keys(paper: Paper) -> list[str]:
    """Keys this paper can be looked up by, so records without a DOI still link up."""
    keys = []
    if paper.doi:
        keys.append(f"doi:{paper.doi}")
    title = normalize_title(paper.title)
    if title:
        keys.append(f"t:{title}|{paper.year}")
        keys.append(f"t:{title}")   # also absorbs a mismatched year
    return keys


def _combine(base: Paper, other: Paper) -> Paper:
    """Fill in `base` with fields from `other`. `base` is the higher-priority one."""
    if not base.doi and other.doi:
        base.doi = other.doi
    if len(other.abstract) > len(base.abstract):
        base.abstract = other.abstract
        base.abstract_source = other.abstract_source
    if not base.publisher:
        base.publisher = other.publisher
    if not base.venue:
        base.venue = other.venue
    if base.year is None:
        base.year = other.year
    if len(other.authors) > len(base.authors):
        base.authors = other.authors
    if other.cited_by is not None:
        base.cited_by = max(base.cited_by or 0, other.cited_by)
    for rel, href in other.links.items():
        base.links.setdefault(rel, href)
    for src in other.sources:
        if src not in base.sources:
            base.sources.append(src)
    return base


def merge(results: dict[str, list[Paper]]) -> list[Paper]:
    """Per-provider result sets -> a single deduplicated list.

    Ordered "seen by multiple providers" first, then by citation count — a
    paper that multiple providers agree on is more likely to match the query.
    """
    merged: list[Paper] = []
    index: dict[str, Paper] = {}

    ordered: list[Paper] = []
    for name in _PRIORITY:
        ordered.extend(results.get(name, []))
    for name, papers in results.items():          # also honor providers not in the list
        if name not in _PRIORITY:
            ordered.extend(papers)

    for paper in ordered:
        keys = _alias_keys(paper)
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            merged.append(paper)
            for k in keys:
                index.setdefault(k, paper)
        else:
            if _rank(paper) < _rank(hit):
                # a higher-priority provider arrived later — switch the bibliographic data to it
                combined = _combine(paper, hit)
                merged[merged.index(hit)] = combined
                for k in _alias_keys(combined):
                    index[k] = combined
            else:
                _combine(hit, paper)
                for k in keys:
                    index.setdefault(k, hit)

    merged.sort(key=lambda p: (-len(p.sources), -(p.cited_by or 0)))
    # merging may have filled in a DOI, so recompute the id for consistency.
    for paper in merged:
        paper.id = paper.make_id()
    return merged
