"""Provider registry and parallel execution.

  openalex   the official API. Default path. 10,000 requests/day with a free key.
  gscholar   no official API, so it's scraped via a browser. Slow and prone to
             blocking — a secondary path.
  dblp       manually curated, CS-only bibliography. No full-text index and no
             abstracts, but often has recent conference proceedings that
             OpenAlex hasn't ingested at all (confirmed for USENIX Security
             2022+) — a second, independent net rather than a replacement.

Semantic Scholar used to be here too but was removed: its venue aliasing
collapses distinct journals into one (measured: half the results for a
specific conference were unrelated papers), which made it unusable for
venue-based collection.
"""

from __future__ import annotations

import asyncio

from . import dblp as db
from . import gscholar as gs
from . import openalex as oa
from .filters import apply_filter
from .models import SCOPE_ALL, Paper
from .query import source_only_words, split_source_terms

OPENALEX = "openalex"
GSCHOLAR = "gscholar"
DBLP = "dblp"

PROVIDER_LABELS = {
    OPENALEX: "OpenAlex",
    GSCHOLAR: "Google Scholar",
    DBLP: "DBLP",
}
# Every provider runs on every search now -- there's no per-search opt-in
# toggle in the GUI any more, just a post-search show/hide by source tag.
# Google Scholar's own per-result sleep (see gscholar.py) makes it far slower
# than the other two, which is why search_gscholar clamps its own count
# independently of max_results instead of trying to honor the full ceiling.
DEFAULT_PROVIDERS = (OPENALEX, DBLP, GSCHOLAR)

MAX_RESULTS = 1000        # per-provider ceiling for a single "search"
# The first response used to try for the full MAX_RESULTS ceiling before
# anything showed up, and getting the rest meant clicking "load more" by
# hand. Now round 1 only fetches this many -- fast, since it no longer waits
# on paging deep into any one provider -- and the GUI keeps fetching further
# batches on its own as the user scrolls near the bottom of the list (see
# app.js), with no button and no upper prompt; PAGE_BATCH is the size of
# each of those follow-up fetches.
INITIAL_BATCH = 50
PAGE_BATCH = 50


class ProviderError(RuntimeError):
    pass


async def search_openalex(
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    cursor: str | None = None,
):
    return await oa.search(query, max_results, year_from, year_to, cursor=cursor)


async def search_gscholar(
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    fetch_abstracts: bool = False,
):
    # author:/intitle: are genuine Scholar operators, so those pass straight
    # through. source: is this tool's own invention -- Scholar has no venue
    # operator at all, so sending it as literal query text would just search
    # for the words "source: ..." instead of filtering by venue. It's pulled
    # out here and re-checked locally instead (Scholar's parsed results do
    # carry a venue field, so the same field-scoped local check as OpenAlex's
    # residual path works unchanged).
    scholar_query, source_only = split_source_terms(query)
    # A venue-only query (very common in this tool -- a formal name split
    # across several source: tokens per spec 2.1) strips to nothing here,
    # and Scholar refuses an empty query -- confirmed live: 'source:USENIX'
    # alone silently returned 0 results from Scholar, no error, nothing to
    # explain why. Falling back to the venue's own words as plain keywords
    # gives it something to search by; the source_only recheck below still
    # enforces the actual venue match on whatever comes back.
    if not scholar_query.strip() and source_only:
        scholar_query = source_only_words(query)
    # Scholar's own deliberate per-result sleep (5-10s, see gscholar.py) makes
    # honoring the full max_results ceiling impractical now that every search
    # runs it -- capped independently so "always on" doesn't mean "always
    # wait tens of minutes".
    papers, note = await gs.search(scholar_query, min(max_results, gs.MAX_RESULTS))
    if fetch_abstracts and papers:
        from .scholar import enrich_abstracts
        await enrich_abstracts(papers)
    if source_only:
        before = len(papers)
        papers = apply_filter(papers, source_only, SCOPE_ALL)
        if len(papers) != before:
            note = (
                f"{note} venue filter narrowed {before} results down to {len(papers)} "
                f"(checked locally -- Scholar has no venue operator)."
            ).strip()
    # Scholar handles keyword syntax natively but has no date-range syntax (spec 2.4).
    if year_from is not None or year_to is not None:
        papers = apply_filter(papers, "", SCOPE_ALL, year_from, year_to)
    return papers, note, None, len(papers)


async def search_dblp(
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    fetch_abstracts: bool = False,
):
    papers, note = await db.search(query, max_results, year_from, year_to)
    # DBLP's own search is fuzzy recall only (see dblp.py's module docstring)
    # -- the same local filter spec 5's refine search already uses re-checks
    # the rest of the original query against what came back. source: is
    # deliberately excluded from this pass: dblp.py already matched the venue
    # by DBLP's own stream key (its equivalent of OpenAlex's source ID), which
    # is reliable specifically *because* it doesn't depend on the venue's
    # display text -- and that text is exactly what this word-substring check
    # would have to use. Confirmed for IEEE RE: DBLP's own per-paper venue
    # field is just the acronym 'RE', so checking the full typed name
    # ('IEEE International Conference on Requirements Engineering') against
    # it word-by-word would reject a paper the stream-key match already
    # correctly confirmed.
    if papers:
        before = len(papers)
        non_source_query, _ = split_source_terms(query)
        papers = apply_filter(papers, non_source_query, SCOPE_ALL, year_from, year_to)
        if len(papers) != before:
            note = f"{note} kept {len(papers)} of {before} after locally re-verifying the query.".strip()
    if fetch_abstracts and papers:
        from .scholar import enrich_abstracts
        await enrich_abstracts(papers)
    return papers, note, None, len(papers)


async def gather_providers(
    providers: list[str],
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    fetch_abstracts: bool = False,
    cursors: dict[str, str] | None = None,
) -> tuple[dict[str, list[Paper]], list[str], list[str], dict[str, str], dict[str, int]]:
    """Query the selected providers concurrently.

    If one fails, the rest are kept — Google Scholar in particular gets
    blocked often, and that shouldn't sink the whole search.

    Returns: (per-provider results, notes, failed providers, next cursors,
    per-provider totals). A provider that succeeded with 0 results is
    distinguished from one whose request actually failed.
    """
    cursors = cursors or {}

    async def run(name: str):
        if name == OPENALEX:
            return await search_openalex(
                query, max_results, year_from, year_to, cursors.get(name)
            )
        if name == GSCHOLAR:
            return await search_gscholar(
                query, max_results, year_from, year_to, fetch_abstracts
            )
        if name == DBLP:
            return await search_dblp(
                query, max_results, year_from, year_to, fetch_abstracts
            )
        raise ProviderError(f"unknown provider: {name}")

    outcomes = await asyncio.gather(*(run(n) for n in providers), return_exceptions=True)

    results: dict[str, list[Paper]] = {}
    notes: list[str] = []
    failed: list[str] = []
    next_cursors: dict[str, str] = {}
    totals: dict[str, int] = {}

    for name, outcome in zip(providers, outcomes):
        label = PROVIDER_LABELS.get(name, name)
        if isinstance(outcome, BaseException):
            if isinstance(outcome, (oa.OpenAlexError, gs.GScholarError, db.DblpError, ProviderError)):
                notes.append(f"{label}: {outcome}")
            else:
                notes.append(f"{label}: request failed ({type(outcome).__name__}: {outcome})")
            failed.append(name)
            results[name] = []
            totals[name] = 0
            continue

        papers, note, cursor, total = outcome
        results[name] = papers
        totals[name] = total or len(papers)
        if cursor:
            next_cursors[name] = cursor
        if note:
            notes.append(f"{label}: {note}")

    return results, notes, failed, next_cursors, totals
