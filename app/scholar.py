"""Google Scholar result HTML parser.

Fetching (network I/O) is handled by `gscholar.py` via scholarly and
undetected-chromedriver. This module only turns the HTML it receives into
Paper objects. The parser is validated against a real Scholar response
(tests/fixtures) and is reused as-is on the browser path.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from .models import Paper

YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
CITED_RE = re.compile(r"(\d+)")


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _split_meta(meta: str) -> tuple[list[str], str, int | None, str]:
    """Parse the '.gs_a' line.

    Format: "J Smith, A Jones - Journal of X, 2005 - publisher.com"
    """
    parts = [p.strip() for p in meta.split(" - ")]
    authors_raw = parts[0] if parts else ""
    middle = parts[1] if len(parts) > 1 else ""
    publisher = parts[2] if len(parts) > 2 else ""

    authors = [
        _clean(a).replace("…", "").strip()
        for a in re.split(r",|;", authors_raw)
        if _clean(a).strip(" …")
    ]

    year = None
    m = YEAR_RE.search(middle)
    if m:
        year = int(m.group(1))
        venue = _clean(middle[: m.start()].rstrip(" ,-"))
    else:
        venue = _clean(middle)
        m2 = YEAR_RE.search(publisher)
        if m2:
            year = int(m2.group(1))

    return authors, venue, year, _clean(publisher)


def parse_results(html: str) -> list[Paper]:
    soup = BeautifulSoup(html, "lxml")
    papers: list[Paper] = []

    for block in soup.select("div.gs_r.gs_or.gs_scl, div.gs_r[data-cid]"):
        title_el = block.select_one(".gs_rt")
        if title_el is None:
            continue

        # strip badges like [PDF] [BOOK]
        for badge in title_el.select(".gs_ctu, .gs_ctc, .gs_ct1, .gs_ct2"):
            badge.decompose()

        anchor = title_el.find("a")
        title = _clean(anchor.get_text() if anchor else title_el.get_text())
        if not title:
            continue

        links: dict[str, str] = {}
        if anchor and anchor.get("href"):
            links["primary"] = anchor["href"]

        pdf = block.select_one(".gs_ggsd a[href]")
        if pdf:
            links["pdf"] = pdf["href"]

        cited_by = None
        for a in block.select(".gs_fl a[href]"):
            text = _clean(a.get_text())
            href = a["href"]
            if href.startswith("/"):
                href = "https://scholar.google.com" + href
            low = text.lower()
            if low.startswith("cited by"):
                links["citations"] = href
                m = CITED_RE.search(text)
                if m:
                    cited_by = int(m.group(1))
            elif "version" in low:
                links["versions"] = href
            elif low.startswith("related"):
                links["related"] = href

        authors, venue, year, publisher = _split_meta(
            _clean(block.select_one(".gs_a").get_text()) if block.select_one(".gs_a") else ""
        )

        snippet_el = block.select_one(".gs_rs")
        snippet = _clean(snippet_el.get_text()) if snippet_el else ""

        papers.append(
            Paper(
                title=title,
                authors=authors,
                publisher=publisher,
                venue=venue,
                year=year,
                abstract=snippet,
                abstract_source="snippet" if snippet else "",
                links=links,
                cited_by=cited_by,
            )
        )
    return papers


# --------------------------------------------------------------------------- #
# Abstract enrichment (spec 3 — "when available")
# --------------------------------------------------------------------------- #
ABSTRACT_META = (
    ("meta", {"name": "citation_abstract"}),
    ("meta", {"name": "description"}),
    ("meta", {"property": "og:description"}),
    ("meta", {"name": "dc.description"}),
    ("meta", {"name": "DC.Description"}),
)


def _extract_abstract(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag, attrs in ABSTRACT_META:
        el = soup.find(tag, attrs=attrs)
        if el and el.get("content") and len(el["content"]) > 120:
            return _clean(el["content"])

    for sel in ("#abstract", ".abstract", "section.abstract", "div.abstractSection", "#Abs1-content"):
        el = soup.select_one(sel)
        if el:
            text = _clean(el.get_text(" "))
            if len(text) > 120:
                return text[:6000]
    return ""


async def _enrich_one(session: AsyncSession, paper: Paper, sem: asyncio.Semaphore) -> None:
    url = paper.links.get("primary") or paper.links.get("pdf")
    if not url or not url.startswith("http"):
        return
    if urlparse(url).path.lower().endswith(".pdf"):
        return
    async with sem:
        try:
            resp = await session.get(url, timeout=12.0, impersonate="safari17_0")
            ctype = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "html" in ctype:
                text = _extract_abstract(resp.text)
                if len(text) > len(paper.abstract):
                    paper.abstract = text
                    paper.abstract_source = "fulltext"
        except Exception:
            pass  # abstract enrichment is strictly best-effort


async def enrich_abstracts(papers: list[Paper], concurrency: int = 4) -> None:
    sem = asyncio.Semaphore(concurrency)
    async with AsyncSession(max_clients=concurrency) as session:
        await asyncio.gather(*(_enrich_one(session, p, sem) for p in papers))
