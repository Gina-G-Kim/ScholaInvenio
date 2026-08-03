"""DBLP collector.

DBLP is a manually curated, CS-only bibliography (dblp.org) that is often
*more* complete than OpenAlex for recent conference proceedings. Confirmed
directly: OpenAlex has essentially stopped ingesting USENIX Security papers
since 2022 (2-6 works/year, none of them linked to any source, and the
individual papers aren't findable under any source, linked or not -- OpenAlex
appears to just not have them). DBLP has the full 2024 program the same day
the proceedings are up. It has no full-text index and no abstracts, so it's
used as a second, independent net rather than a replacement for OpenAlex:
what one misses, the other tends to have.

  - No API key. Public, CC0-licensed data (dblp.org/faq).
  - Search:  GET /search/publ/api?q=...&format=json  (title/author text
             search; `author:X:` and `year:X:` are query facets, not
             separate params). Capped at 100 hits per request regardless of
             `h`; page further with `f=<offset>`.
  - Venues:  GET /search/venue/api?q=...&format=json  resolves a name to
             DBLP's own venue record(s). Its `venue` display string is
             *not* reliable for matching individual papers against, though:
             confirmed for IEEE RE, the venue-search API returns the long
             descriptive name ('IEEE International Requirements Engineering
             Conference (RE)'), but individual papers' own `venue` field is
             often just the bare acronym ('RE') -- the two don't even
             overlap as text. What every paper in a venue reliably shares
             instead is its DBLP *key* prefix (the part of e.g.
             'conf/re/Hielscher24' before the second slash), taken from the
             venue record's own `url` -- that's the mechanism used here, the
             direct equivalent of OpenAlex's `locations.source.id`.
  - robots.txt sets Crawl-delay: 4 for the site; paginated requests here are
    spaced out to stay a good citizen of a free, ad-free, volunteer-run service.
  - Mirrored at dblp.org and dblp.uni-trier.de (same organization); the
    primary domain has been seen returning transient 5xxs, so a request
    falls back to the mirror before giving up.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime

from curl_cffi.requests import AsyncSession

from .filters import in_year_range
from .models import Paper
from .query import OrGroup, parse_query
from .venue_match import looks_like_same_venue

HOSTS = ("https://dblp.org", "https://dblp.uni-trier.de")
PAGE_SIZE = 100          # confirmed ceiling regardless of the requested `h`
REQUEST_PAUSE = 1.0      # seconds between our own paginated requests
MAX_PAGES = 10           # safety valve: at most 1,000 works examined per query
# DBLP's `year:` facet only takes one exact year -- confirmed 'year:2024-2026:'
# is read as a single literal value, not a range, and returns nothing. A
# bounded year_from/year_to is instead queried one year at a time natively;
# past this many years it falls back to one broad query with only the local
# in_year_range check, since asking a query per year without limit doesn't scale.
MAX_YEAR_FACETS = 6

_DISAMBIGUATOR_RE = re.compile(r"\s+\d{4}$")
_DOI_HOST_RE = re.compile(r"^https?://doi\.org/", re.IGNORECASE)
_STREAM_KEY_RE = re.compile(r"/db/(.+?)/?$")


class DblpError(RuntimeError):
    pass


def _clean_name(name: str) -> str:
    """'Wenyuan Xu 0001' -> 'Wenyuan Xu' -- DBLP's own disambiguation suffix."""
    return _DISAMBIGUATOR_RE.sub("", html.unescape(name or "")).strip()


def _as_list(x) -> list:
    """DBLP's JSON doesn't wrap a single child in a list -- a common XML->JSON quirk."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _as_text(x) -> str:
    """A field like `venue` is sometimes a list (a workshop plus its shared
    proceedings series, e.g. ['RE4SuSy@RE', 'CEUR Workshop Proceedings']) --
    the first entry is the specific one."""
    if isinstance(x, list):
        x = x[0] if x else ""
    return html.unescape(x or "").strip()


async def _get(session: AsyncSession, path: str, params: dict) -> dict:
    last_exc: Exception | None = None
    for host in HOSTS:
        try:
            resp = await session.get(f"{host}{path}", params={**params, "format": "json"}, timeout=30.0)
            if resp.status_code == 200:
                return resp.json() or {}
            last_exc = DblpError(f"DBLP error {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:  # noqa: BLE001 -- try the mirror on any failure
            last_exc = exc
    raise DblpError(str(last_exc) if last_exc else "DBLP request failed")


# --------------------------------------------------------------------------- #
# Venue name -> DBLP stream key(s)
# --------------------------------------------------------------------------- #
async def resolve_venue(session: AsyncSession, name: str) -> list[tuple[str, str, str]]:
    """Venue name -> [(stream key, display name, acronym), ...] for the venues that match.

    The display name (from /search/venue/api) is a clean, canonical string
    with no year or edition number, so venue_match.looks_like_same_venue can
    tell a same-family workshop from a sister venue the same way it does for
    OpenAlex. But neither that string nor the acronym is trusted on its own
    to fetch papers -- see _fetch_venue_papers and the module docstring for
    why. The stream key is what actually confirms a paper belongs here.
    """
    data = await _get(session, "/search/venue/api", {"q": name, "h": "20"})
    hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for h in hits:
        info = h.get("info") or {}
        venue = _as_text(info.get("venue"))
        acronym = _as_text(info.get("acronym"))
        url = info.get("url") or ""
        m = _STREAM_KEY_RE.search(url)
        stream = m.group(1) if m else ""
        if venue and stream and stream not in seen and looks_like_same_venue(venue, name):
            seen.add(stream)
            found.append((stream, venue, acronym))
    return found


# --------------------------------------------------------------------------- #
# Query -> best-effort DBLP recall terms
#
# DBLP's own text search is a fuzzy, prefix-based index over title/author/
# venue combined, with no notion of phrase-exact matching, negation, or
# wildcards, and there's no abstract to search at all. So rather than
# reproducing translate.py's provider-native-filter/residual split, this only
# uses DBLP to fetch a generous recall set (free words for `q=`, `author:`
# for author: terms, the venue's own words as plain keywords for source:
# terms), then the *entire* original query is re-applied locally exactly as
# spec 5's refine search already does -- correctness comes from that same
# proven local filter plus the stream-key check below, not from trusting
# DBLP's fuzzy match.
# --------------------------------------------------------------------------- #
def _recall_terms(text: str) -> tuple[list[str], list[str], str]:
    free: list[str] = []
    author_words: list[str] = []
    venue_words: list[str] = []
    for node in parse_query(text):
        terms = node.terms if isinstance(node, OrGroup) else [node]
        for t in terms:
            if t.negated:
                continue
            words = [w for w in t.value.replace("*", " ").split() if w]
            if not words:
                continue
            if t.field == "source":
                venue_words.extend(words)
            elif t.field == "author":
                author_words.extend(words)
            else:
                free.extend(words)
    return free, author_words, " ".join(venue_words)


def _parse_hit(info: dict) -> Paper | None:
    if (info.get("type") or "") == "Editorship":
        return None  # the proceedings volume record itself, not a paper

    title = html.unescape(info.get("title") or "").strip().rstrip(".")
    if not title:
        return None

    authors = [_clean_name(a.get("text", "")) for a in _as_list((info.get("authors") or {}).get("author"))]
    authors = [a for a in authors if a]

    year_raw = info.get("year")
    year = int(year_raw) if year_raw and str(year_raw).isdigit() else None

    doi = (info.get("doi") or "").strip()
    ee = info.get("ee") or ""
    rec_url = info.get("url") or ""
    links: dict[str, str] = {}
    if ee:
        links["primary"] = ee
        if doi or _DOI_HOST_RE.match(ee):
            links["doi"] = ee
    if rec_url:
        links.setdefault("primary", rec_url)
        links["dblp"] = rec_url

    return Paper(
        title=title,
        authors=authors,
        publisher=_as_text(info.get("publisher")),
        venue=_as_text(info.get("venue")),
        year=year,
        abstract="",              # DBLP is bibliographic only -- no abstracts
        abstract_source="",
        links=links,
        cited_by=None,             # DBLP doesn't track citation counts
        doi=doi or links.get("doi", ""),
        sources=["dblp"],
    )


def _year_list(year_from: int | None, year_to: int | None) -> list[int] | None:
    """A concrete list of years to query natively, or None if there's no
    range (search everything) or the range is too wide to query year-by-year."""
    if year_from is None and year_to is None:
        return None
    lo = year_from if year_from is not None else (year_to - MAX_YEAR_FACETS + 1)
    hi = year_to if year_to is not None else datetime.now().year
    if hi < lo or hi - lo + 1 > MAX_YEAR_FACETS:
        return None
    return list(range(lo, hi + 1))


async def _fetch_publ(session: AsyncSession, q: str, cap: int) -> list[dict]:
    collected: list[dict] = []
    offset = 0
    for page in range(MAX_PAGES):
        if len(collected) >= cap:
            break
        data = await _get(session, "/search/publ/api", {
            "q": q, "h": str(PAGE_SIZE), "f": str(offset),
        })
        hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
        if not hits:
            break
        collected.extend(h.get("info") or {} for h in hits)
        sent = int(((data.get("result") or {}).get("hits") or {}).get("@sent") or len(hits))
        if sent < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        if page < MAX_PAGES - 1:
            await asyncio.sleep(REQUEST_PAUSE)
    return collected[:cap]


async def _fetch_venue_papers(
    session: AsyncSession,
    stream: str,
    display: str,
    acronym: str,
    base_words: list[str],
    years: list[int] | None,
    cap: int,
) -> list[dict]:
    """One matched venue's papers, found by whichever recall query actually works.

    Confirmed directly, for two real venues: DBLP's per-paper `venue` field
    is sometimes the long descriptive name (USENIX Security Symposium) and
    sometimes just a bare acronym (IEEE RE's papers all say 'RE', not the
    long name /search/venue/api returns for it) -- with no way to know which
    ahead of time. A `venue:<acronym>:` facet query only works for the
    latter case (using the long name there found 1 paper out of ~2,000; the
    acronym found all of them), so the acronym is tried first when DBLP
    offers one, and the long name is only tried as a second pass if that
    didn't turn up much -- typically one request per venue, not a fixed two.
    Either way, only the stream-key prefix on each hit's own `key` decides
    what actually counts as this venue; both queries are pure recall.
    """
    async def fetch_variant(extra_words: list[str]) -> list[dict]:
        hits: list[dict] = []
        for i, y in enumerate(years or [None]):
            if i:
                await asyncio.sleep(REQUEST_PAUSE)
            q = " ".join(base_words + extra_words + ([f"year:{y}:"] if y is not None else []))
            hits.extend(await _fetch_publ(session, q, cap))
        return [info for info in hits if (info.get("key") or "").startswith(f"{stream}/")]

    result: list[dict] = []
    if acronym:
        result = await fetch_variant([f"venue:{acronym}:"])
    if len(result) < 5 and display:
        if acronym:
            await asyncio.sleep(REQUEST_PAUSE)
        more = await fetch_variant(display.split())
        seen_keys = {info.get("key") for info in result}
        result.extend(info for info in more if info.get("key") not in seen_keys)
    return result[:cap]


async def search(
    query: str,
    max_results: int,
    year_from: int | None = None,
    year_to: int | None = None,
    scope: str = "all",
) -> tuple[list[Paper], str]:
    """Returns (papers, note). No cursor/load-more support (see module docstring)."""
    free, author_words, venue_name = _recall_terms(query)
    if not free and not author_words and not venue_name:
        return [], ""

    years = _year_list(year_from, year_to)
    note = ""
    venues: list[tuple[str, str, str]] = []  # (stream key, display name, acronym)
    async with AsyncSession() as session:
        if venue_name:
            try:
                venues = await resolve_venue(session, venue_name)
            except DblpError as exc:
                return [], f"DBLP venue lookup failed: {exc}"
            if venues:
                note = f"venue '{venue_name}' -> DBLP: " + ", ".join(d for _, d, _ in venues)
            else:
                note = f"could not find venue '{venue_name}' on DBLP; searched by keyword instead."

        base_words = list(free)
        if author_words:
            base_words.append("author:" + " ".join(author_words) + ":")

        raw_hits: list[dict] = []
        if venues:
            # A broad name like 'USENIX' can resolve to several real, distinct
            # venues -- split the budget so one doesn't crowd out the rest,
            # same reasoning as OpenAlex's per-source cap.
            cap_each = max(10, max_results // len(venues))
            for i, (stream, display, acronym) in enumerate(venues):
                if i:
                    await asyncio.sleep(REQUEST_PAUSE)
                raw_hits.extend(await _fetch_venue_papers(
                    session, stream, display, acronym, base_words, years, cap_each
                ))
        else:
            # No venue resolved (or none asked for) -- best-effort keyword
            # search only; nothing to check a stream key against.
            words = list(base_words)
            if venue_name:
                words.extend(venue_name.split())
            for i, y in enumerate(years or [None]):
                if i:
                    await asyncio.sleep(REQUEST_PAUSE)
                q = " ".join(words + ([f"year:{y}:"] if y is not None else []))
                raw_hits.extend(await _fetch_publ(session, q, max_results))

    papers: list[Paper] = []
    seen: set[str] = set()
    for info in raw_hits:
        paper = _parse_hit(info)
        if not paper or not in_year_range(paper, year_from, year_to):
            continue
        if paper.id in seen:
            continue
        seen.add(paper.id)
        papers.append(paper)

    return papers[:max_results], note
