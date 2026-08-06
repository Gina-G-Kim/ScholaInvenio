"""OpenAlex collector.

Confirmed against the official docs (developers.openalex.org):

  - Auth:  the `api_key` parameter. Free to get at openalex.org/settings/api.
  - Usage: $1/day with a key, $0.01/day without. At $0.0001 per request that's
           **100 requests/day without a key, 10,000 with one** — a key is
           effectively required.
  - Names are ambiguous, IDs are not. The docs explicitly recommend resolving
    a name to an ID first, so venues are also resolved via /sources before filtering.
  - Deep pagination uses a cursor (the `page` parameter caps out at 10,000 results).
  - Inside `filter`, OR is `|` (max 100 values), NOT is `!`, ranges are `2020-2024`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from curl_cffi.requests import AsyncSession

from .filters import apply_filter
from .models import SCOPE_ALL, Paper
from .translate import to_openalex
from .venue_match import clean_venue_title, looks_like_same_venue

WORKS_URL = "https://api.openalex.org/works"
SOURCES_URL = "https://api.openalex.org/sources"

PER_PAGE = 200           # measured ceiling
MAX_FILTER_VALUES = 100  # max values a `filter`'s `|` can OR together

# Only request the fields we need, to keep the response small.
SELECT_FIELDS = ",".join((
    "id", "doi", "title", "display_name", "publication_year",
    "authorships", "primary_location", "best_oa_location", "locations",
    "cited_by_count", "abstract_inverted_index", "type",
))

# If matched venues' combined paper count is below this, the query is treated
# as too narrow and relaxed.
_WEAK_SOURCE_WORKS = 100

# A matched venue is kept only if its relevance_score is at least this
# fraction of the top match's. OpenAlex's /sources search returns matches
# ranked by actual text relevance, but a huge, loosely-related source (a
# series with hundreds of thousands of works) can still show up somewhere in
# the list. Sorting by works_count instead of relevance used to let that
# outlier jump to first place and dominate the filter (measured: searching
# 'Artificial Intelligence' let 'Lecture Notes in Computer Science', ranked
# 22nd by relevance, outrank the actual journal because it has 605,961 works
# against the journal's 4,828). This cutoff keeps the ranking honest.
_MIN_RELEVANCE_RATIO = 0.20


class OpenAlexError(RuntimeError):
    pass


class OpenAlexBudgetExhausted(OpenAlexError):
    """Daily usage budget exhausted (429). An API key raises the ceiling 100x."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _auth_params() -> dict[str, str]:
    params: dict[str, str] = {}
    key = _env("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    mailto = _env("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    return params


def has_api_key() -> bool:
    return bool(_env("OPENALEX_API_KEY"))


def _check(resp) -> None:
    if resp.status_code == 200:
        return
    body = ""
    try:
        body = str((resp.json() or {}).get("message") or resp.text)
    except Exception:
        body = resp.text or ""
    if resp.status_code == 429:
        hint = (
            "OpenAlex daily usage budget is exhausted. "
            + ("It resets at midnight UTC." if has_api_key() else
               "Get a free API key at openalex.org/settings/api and set it as "
               "OPENALEX_API_KEY — that raises the limit from $0.01/day to $1/day "
               "(100 requests -> 10,000).")
        )
        raise OpenAlexBudgetExhausted(f"{hint} (upstream: {body[:160]})")
    raise OpenAlexError(f"OpenAlex error {resp.status_code}: {body[:200]}")


# --------------------------------------------------------------------------- #
# Abstract reconstruction
# --------------------------------------------------------------------------- #
def reconstruct_abstract(inverted: dict | None) -> str:
    """OpenAlex gives the abstract as an inverted index; reconstruct the text from word positions."""
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


def _matched_location(work: dict, source_ids: set[str] | None) -> dict:
    """When searching by venue, pick the location that actually matches it.

    Filtering happens on `locations.source.id`, so `primary_location` can be a
    different venue entirely. Showing the primary venue's name in that case
    reads as "I searched USENIX and got some unrelated journal" — so the
    matched location takes priority.
    """
    if not source_ids:
        return work.get("primary_location") or {}
    for loc in [work.get("primary_location") or {}] + list(work.get("locations") or []):
        src = loc.get("source") or {}
        sid = (src.get("id") or "").rsplit("/", 1)[-1]
        if sid and sid in source_ids:
            return loc
    return work.get("primary_location") or {}


def to_paper(work: dict, source_ids: set[str] | None = None) -> Paper | None:
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None

    authors = [
        a["author"]["display_name"]
        for a in (work.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    ]

    loc = _matched_location(work, source_ids)
    src = loc.get("source") or {}
    venue = (src.get("display_name") or "").strip()
    publisher = (src.get("host_organization_name") or "").strip()

    # if the matched location is missing data, fill in from another location
    if not venue or not publisher:
        for other in list(work.get("locations") or []) + [work.get("primary_location") or {}]:
            osrc = other.get("source") or {}
            venue = venue or (osrc.get("display_name") or "").strip()
            publisher = publisher or (osrc.get("host_organization_name") or "").strip()
            if venue and publisher:
                break

    links: dict[str, str] = {}
    if work.get("doi"):
        links["doi"] = work["doi"]
    if loc.get("landing_page_url"):
        links["primary"] = loc["landing_page_url"]
    elif (work.get("primary_location") or {}).get("landing_page_url"):
        links["primary"] = work["primary_location"]["landing_page_url"]
    best = work.get("best_oa_location") or {}
    if best.get("pdf_url"):
        links["pdf"] = best["pdf_url"]
    elif loc.get("pdf_url"):
        links["pdf"] = loc["pdf_url"]
    if work.get("id"):
        links.setdefault("primary", work["id"])
        links["openalex"] = work["id"]

    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    return Paper(
        title=title,
        authors=authors,
        publisher=publisher,
        venue=venue,
        year=work.get("publication_year"),
        abstract=abstract,
        abstract_source="api" if abstract else "",
        links=links,
        cited_by=work.get("cited_by_count"),
        doi=work.get("doi") or "",
        sources=["openalex"],
    )


# --------------------------------------------------------------------------- #
# Venue name -> source ID
# --------------------------------------------------------------------------- #
async def _lookup_sources(session: AsyncSession, name: str, limit: int) -> list[dict]:
    params = {
        "search": name,
        "per-page": str(limit),
        "select": "id,display_name,works_count,abbreviated_title,alternate_titles,relevance_score",
        **_auth_params(),
    }
    resp = await session.get(SOURCES_URL, params=params, timeout=40.0)
    _check(resp)
    # OpenAlex already returns these ordered by relevance_score (highest first).
    return (resp.json() or {}).get("results") or []


def _drop_irrelevant(results: list[dict]) -> list[dict]:
    """Cut off matches whose relevance_score trails too far behind the top one.

    A real match set decays gradually (Nature, Nature Communications, Nature
    Genetics: 100%, 47%, 30%, ...). An unrelated giant source sits far below
    that curve even though its works_count dwarfs everything else, so a
    relevance-ratio cutoff catches what a works_count sort would have missed.
    """
    if not results:
        return results
    top = results[0].get("relevance_score") or 0
    if not top:
        return results
    return [r for r in results if (r.get("relevance_score") or 0) >= top * _MIN_RELEVANCE_RATIO]


async def resolve_sources(
    session: AsyncSession, name: str, limit: int = MAX_FILTER_VALUES
) -> tuple[list[str], list[str]]:
    """Venue name -> (source IDs, human-readable match description).

    /sources search looks at `display_name` as well as `abbreviated_title` and
    `alternate_titles`, so abbreviations are naturally handled by OpenAlex's
    own index — there's no need to maintain an abbreviation table here.

    Three pitfalls are handled:
      1) search ANDs its tokens together. A full formal name like 'IEEE
         International Conference on Requirements Engineering' only matches
         records containing every one of those words, and OpenAlex commonly
         splits the same conference across a couple of source records by
         naming variant, so a single dropped leading token can be needed to
         catch the second one. But relaxing indefinitely is dangerous: once
         enough leading words are gone, what's left can be generic enough to
         match completely unrelated fields (confirmed live: 'IEEE
         International Requirements Engineering Conference' correctly
         matches the real conference on the very first, full-length query --
         only 14 works, since OpenAlex's coverage of it is thin -- but
         relaxing three more steps down to 'Engineering Conference' pulled
         in a materials-science journal, a petroleum-engineering conference,
         and half a dozen other venues that just happen to also be
         "engineering conferences"). So relaxation stops one step after the
         first hit, regardless of how small that hit's works_count is --
         a low works_count is not evidence that a match is wrong, and isn't
         worth risking a wide-open generic query to try to pad out.
      2) OpenAlex splits the same conference across several source records by
         year or by naming variant.
      3) A huge, only loosely related source (see `_drop_irrelevant`) can
         still appear somewhere in the match list and must not be allowed to
         dominate the filter just because it has more works than everything
         else combined.
    """
    tokens = name.split()
    found: list[dict] = []
    total = 0
    hit_at: int | None = None

    for cut in range(0, max(len(tokens) - 1, 1)):
        candidate = " ".join(tokens[cut:])
        batch = _drop_irrelevant(await _lookup_sources(session, candidate, min(limit, 50)))
        seen = {r.get("id") for r in found}
        found += [r for r in batch if r.get("id") not in seen]
        total = sum(r.get("works_count") or 0 for r in found)
        if found and hit_at is None:
            hit_at = cut
        if total >= _WEAK_SOURCE_WORKS:
            break
        if hit_at is not None and cut >= hit_at + 1:
            break

    found.sort(key=lambda r: -(r.get("relevance_score") or 0))
    found = [r for r in found if (r.get("works_count") or 0) > 0][:limit]

    ids = [r["id"].rsplit("/", 1)[-1] for r in found if r.get("id")]
    described = [f"{r.get('display_name', '?')}({r.get('works_count', 0)})" for r in found[:4]]
    if len(found) > 4:
        described.append(f"and {len(found) - 4} more")
    return ids, described


# --------------------------------------------------------------------------- #
# Editions OpenAlex never linked to a source
#
# Confirmed directly against the API: every work in IEEE Symposium on
# Security and Privacy 2023 and 2024, and many older IEEE International
# Conference on Requirements Engineering editions, has `primary_location`
# (and every other location) with `source: null` -- OpenAlex has the work,
# it just never linked it to a /sources record. `locations.source.id`
# filtering can therefore never surface these, no matter how correctly the
# venue name resolves; the venue lookup can succeed and the works filter
# still comes back empty or missing that edition entirely.
#
# The front-matter ("paratext") entry for the proceedings volume still
# carries the real venue name as its title, and its own DOI is the exact
# prefix every paper in that volume extends, e.g. front matter
# 'doi.org/10.1109/sp54263.2024' with real papers at
# '10.1109/sp54263.2024.00005', '...00179', etc. Finding that one
# front-matter work recovers the whole edition via `doi_starts_with`.
# --------------------------------------------------------------------------- #
_DOI_SCHEME_RE = re.compile(r"^https?://doi\.org/", re.IGNORECASE)


async def _resolve_unlinked_editions(
    session: AsyncSession, name: str, extra_filter: str | None
) -> list[tuple[str, str]]:
    """Venue name -> [(doi prefix, clean edition title), ...] for editions with no source link."""
    filters = ["type:paratext"]
    if extra_filter:
        filters.append(extra_filter)
    params = {
        "filter": ",".join(filters),
        "search": name,
        "per-page": "50",
        "select": "id,title,doi",
        **_auth_params(),
    }
    resp = await session.get(WORKS_URL, params=params, timeout=40.0)
    _check(resp)
    results = (resp.json() or {}).get("results") or []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for w in results:
        title = w.get("title") or ""
        if not looks_like_same_venue(title, name):
            continue
        prefix = _DOI_SCHEME_RE.sub("", w.get("doi") or "").strip()
        if not prefix or prefix in seen:
            continue
        seen.add(prefix)
        found.append((prefix, clean_venue_title(title) or title))
    return found


async def _fetch_by_doi_prefix(
    session: AsyncSession, prefix: str, cap: int, cursor: str | None = None
) -> tuple[list[dict], str | None]:
    """Fetch one unlinked edition's papers directly by its shared DOI prefix.

    Resumes from `cursor` (OpenAlex's own native works cursor for this one
    prefix) and returns the next one, or None once this edition is
    exhausted. Without this, every call restarted from "*" and re-fetched
    the exact same first `cap` works every time -- confirmed live: a round
    capped at 8 works per edition (of 63 actually available for just one
    edition) reported no cursor at all, so "load more" could never reach
    anything past that first batch, the same gap DBLP's own cursor had.
    """
    collected: list[dict] = []
    cur = cursor or "*"
    while len(collected) < cap:
        params = {
            "filter": f"doi_starts_with:{prefix}.,type:!paratext",
            "per-page": str(min(PER_PAGE, cap - len(collected))),
            "cursor": cur,
            "select": SELECT_FIELDS,
            **_auth_params(),
        }
        resp = await session.get(WORKS_URL, params=params, timeout=60.0)
        _check(resp)
        payload = resp.json()
        results = payload.get("results") or []
        if not results:
            cur = None
            break
        collected.extend(results)
        cur = (payload.get("meta") or {}).get("next_cursor")
        if not cur:
            break
    return collected[:cap], cur


def _decode_oa_cursor(cursor: str | None) -> tuple[str | None, dict[str, str]]:
    """Combined cursor -> (general works-search cursor, {doi prefix: its own cursor})."""
    if not cursor:
        return None, {}
    try:
        data = json.loads(cursor)
    except ValueError:
        return None, {}
    return data.get("general"), data.get("editions") or {}


def _encode_oa_cursor(general: str | None, editions: dict[str, str | None]) -> str | None:
    live_editions = {prefix: cur for prefix, cur in editions.items() if cur}
    if not general and not live_editions:
        return None
    return json.dumps({"general": general, "editions": live_editions})


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
async def search(
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    scope: str = "all",
    cursor: str | None = None,
) -> tuple[list[Paper], str, str | None, int]:
    """Returns (papers, note, next cursor, total count).

    Accepts a cursor to resume from — so that clicking "load more" doesn't
    re-fetch what was already seen.
    """
    translated = to_openalex(query, year_from, year_to, scope)
    if not translated.params and not translated.source_lookup:
        return [], "", None, 0

    collected: list[Paper] = []
    residual = translated.residual
    note = ""
    total = 0
    general_cursor_in, edition_cursors_in = _decode_oa_cursor(cursor)
    edition_cursors_out: dict[str, str | None] = {}
    cur = general_cursor_in or "*"
    source_ids: set[str] = set()
    # Once a venue name resolves to several source IDs, cap how many results
    # any single one of them can contribute. Without this, a name like 'IEEE
    # International Conference on Requirements Engineering' matches both the
    # conference (14 works) and the much bigger 'Requirements Engineering'
    # journal (1,078 works) it got merged with, and results just come back in
    # whatever order OpenAlex returns them — which means the journal, having
    # 77x more works, fills the entire page and the conference never appears
    # (measured: 46 of 49 results were journal papers, only 3 were the
    # conference and its naming variant combined).
    per_source_cap: int | None = None
    per_source_count: dict[str, int] = {}
    # Safety valve for the cap above: if one matched venue vastly outnumbers
    # the others, most fetched works get skipped once its share is full, so
    # reaching max_results could mean paging through the venue's entire
    # catalog. Give up gracefully instead of taking forever once this many
    # works have been looked at, and keep whatever was found by then.
    examine_limit = max(max_results * 20, 2000)
    examined = 0

    unlinked_total = 0
    venue_matched = False

    async with AsyncSession() as session:
        base = dict(translated.params)

        if translated.source_lookup:
            ids, described = await resolve_sources(session, translated.source_lookup)
            unlinked = await _resolve_unlinked_editions(session, translated.source_lookup, base.get("filter"))
            total_buckets = len(ids) + len(unlinked)
            if total_buckets > 1:
                per_source_cap = max(5, max_results // total_buckets)

            if ids:
                # locations.source.id is broader than primary_location. Many
                # conference papers have no source under primary_location and
                # only appear under locations, so filtering on primary alone
                # would drop most of them (measured: 625 vs 1222).
                source_ids = set(ids)
                clause = f"locations.source.id:{'|'.join(ids)}"
                base["filter"] = f"{base['filter']},{clause}" if base.get("filter") else clause

            # Each unlinked edition is its own independent request (the API
            # has no OR syntax for doi_starts_with), so a formal name that
            # splinters across a dozen yearly editions would otherwise mean a
            # dozen sequential round trips (measured: 28s at the 1000-result
            # ceiling for IEEE RE's ten unlinked editions). Fetching them
            # concurrently instead keeps this close to the slowest single one.
            cap_each = per_source_cap if per_source_cap is not None else max_results
            edition_batches = await asyncio.gather(
                *(_fetch_by_doi_prefix(session, prefix, cap_each, edition_cursors_in.get(prefix))
                  for prefix, _ in unlinked)
            )

            unlinked_described: list[str] = []
            for (prefix, title), (works, next_edition_cursor) in zip(unlinked, edition_batches):
                edition_cursors_out[prefix] = next_edition_cursor
                added = 0
                for w in works:
                    paper = to_paper(w, None)
                    if paper:
                        # to_paper's own fallback scans every location for a
                        # source name when the matched one has none -- for
                        # these works that finds an unrelated self-archive
                        # copy's host (arXiv, an institutional repository,
                        # ...) instead of the real, source-less venue. The
                        # venue is already known here, so it overrides
                        # unconditionally rather than only filling a blank.
                        paper.venue = title
                        paper.publisher = ""
                        collected.append(paper)
                        added += 1
                if added:
                    unlinked_total += added
                    unlinked_described.append(f"{title}({added})")

            venue_matched = bool(ids or unlinked_described)
            if ids or unlinked_described:
                # Short and skimmable over exhaustive: a name that splinters
                # across a dozen editions used to produce one dense run-on
                # sentence repeating the same "unlinked" qualifier per item,
                # which is exactly the clutter this trims -- the category is
                # stated once below, each item then just needs a name and count.
                segments = []
                if ids:
                    segments.append(f"{len(ids)} known venue{'s' if len(ids) != 1 else ''}: " + ", ".join(described))
                if unlinked_described:
                    shown = unlinked_described[:3]
                    head = ", ".join(shown)
                    extra = len(unlinked_described) - len(shown)
                    if extra > 0:
                        head = f"{head}, +{extra} more"
                    segments.append(f"{len(unlinked_described)} not linked to a source on OpenAlex: {head}")
                note = f"venue '{translated.source_lookup}' -- " + "; ".join(segments)
            elif translated.source_residual:
                residual = f"{residual} {translated.source_residual}".strip()
                note = f"could not find venue '{translated.source_lookup}' on OpenAlex; filtered locally instead."

        if not base and not collected:
            return [], note, None, 0

        empty_page_retries = 0

        while base and len(collected) < max_results and examined < examine_limit:
            params = dict(base)
            # Always ask for a full page. When a per-source cap is active,
            # most works on a page can end up skipped, so sizing the request
            # to "however many results are still needed" would shrink it to
            # almost nothing right when a bigger page is what's needed.
            page_size = PER_PAGE if per_source_cap is not None else min(PER_PAGE, max_results - len(collected))
            params.update({
                "per-page": str(page_size),
                "cursor": cur,
                "select": SELECT_FIELDS,
                **_auth_params(),
            })
            resp = await session.get(WORKS_URL, params=params, timeout=60.0)
            try:
                _check(resp)
            except OpenAlexBudgetExhausted as exc:
                if collected:
                    # keep what was already collected
                    note = f"{note} {exc}".strip()
                    cur = None
                    break
                raise

            payload = resp.json()
            meta = payload.get("meta") or {}
            total = meta.get("count") or total
            results = payload.get("results") or []
            if not results:
                # meta.count says matches exist but this page came back empty.
                # Measured in the wild: a venue lookup resolves fine (source
                # IDs found, works_count > 0 for them), then the very next
                # /works request against those same IDs returns zero results
                # even though meta.count is nonzero — most likely the search
                # index hadn't caught up with the /sources result yet. A
                # short retry clears it up; a genuine zero-result query has
                # meta.count == 0 too, so this never fires for those.
                if (meta.get("count") or 0) > 0 and empty_page_retries < 3:
                    empty_page_retries += 1
                    await asyncio.sleep(1.5)
                    continue
                cur = None
                break
            empty_page_retries = 0

            for work in results:
                examined += 1
                if per_source_cap is not None:
                    loc = _matched_location(work, source_ids)
                    sid = ((loc.get("source") or {}).get("id") or "").rsplit("/", 1)[-1]
                    if sid:
                        if per_source_count.get(sid, 0) >= per_source_cap:
                            if examined >= examine_limit:
                                break
                            continue  # this venue's share of the page is full
                        per_source_count[sid] = per_source_count.get(sid, 0) + 1
                paper = to_paper(work, source_ids)
                if paper:
                    collected.append(paper)
                if len(collected) >= max_results or examined >= examine_limit:
                    break

            cur = meta.get("next_cursor")
            if not cur:
                break
            await asyncio.sleep(0.15)

    total += unlinked_total

    if per_source_cap is not None and 0 < len(collected) < max_results and examined >= examine_limit:
        note = f"{note} Stopped after {examined} works to keep one venue from crowding out the rest -- use \"load more\" to continue.".strip()

    if residual:
        collected = apply_filter(collected, residual, SCOPE_ALL)

    if venue_matched and year_from is not None and len(collected) < 8:
        # A matched venue returning almost nothing for a recent year range
        # isn't necessarily a bug in how the name resolved -- OpenAlex's own
        # ingestion coverage can thin out for a given venue independently of
        # source-linking (confirmed for USENIX: its linked records go from
        # ~100+ works/year to 2-6/year starting 2022, and the individual
        # papers for those years aren't findable under any source, linked or
        # not, because OpenAlex doesn't appear to have ingested them at all --
        # unlike the IEEE case, this isn't a linking gap this tool can bridge
        # locally). Surfacing that plainly beats a silently thin result list.
        # DBLP turned out to have exactly USENIX's missing 2022+ program, so
        # it's suggested first; Scholar is the fallback for non-CS venues DBLP
        # doesn't cover at all.
        note = (
            f"{note} Only {len(collected)} found for this venue from {year_from} onward -- "
            f"if that looks too low, OpenAlex's own coverage may thin out for these years "
            f"regardless of how the venue name resolved; try enabling DBLP (CS venues) or "
            f"Google Scholar too."
        ).strip()

    next_cursor = _encode_oa_cursor(cur, edition_cursors_out)
    return collected[:max_results], note, next_cursor, total
