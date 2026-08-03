"""XML storage (spec 4, 6).

One session = one directory. Rounds pile up as XML files inside it:

    data/sessions/<session-id>/round_01.xml
                              /round_02.xml
                              /session.xml   (session summary index)

The XML is the source of truth. On restart the server re-reads this
directory tree to restore state.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

from .models import Paper, Round, Session

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SESSIONS_DIR = DATA_DIR / "sessions"
_ROUND_RE = re.compile(r"^round_(\d+)\.xml$")

# control characters not allowed in XML 1.0
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _text(parent: ET.Element, tag: str, value, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if value is not None and value != "":
        el.text = _ILLEGAL.sub("", str(value))
    return el


def _pretty(root: ET.Element) -> bytes:
    raw = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def session_dir(session_id: str) -> Path:
    path = (SESSIONS_DIR / session_id).resolve()
    if not str(path).startswith(str(SESSIONS_DIR.resolve())):
        raise ValueError("invalid session id")
    return path


def round_path(session_id: str, number: int) -> Path:
    return session_dir(session_id) / f"round_{number:02d}.xml"


def new_session_id(display_name: str) -> str:
    """Directory name. Reuses the natural-language session name, made path-safe."""
    from .naming import safe_dirname
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{safe_dirname(display_name)}"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def write_round(session_id: str, rnd: Round) -> Path:
    ensure_dirs()
    directory = session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)

    root = ET.Element("scholarSearch", {"version": "1.0"})

    meta = ET.SubElement(root, "meta")
    _text(meta, "sessionId", session_id)
    _text(meta, "round", rnd.number)
    _text(meta, "query", rnd.query)
    _text(meta, "scope", rnd.scope)
    _text(meta, "sourceRound", rnd.source_round)
    period = ET.SubElement(meta, "period")
    _text(period, "from", rnd.year_from)
    _text(period, "to", rnd.year_to)
    _text(meta, "createdAt", rnd.created_at)
    _text(meta, "resultCount", len(rnd.papers))
    _text(meta, "label", rnd.label)
    if rnd.providers:
        provs = ET.SubElement(meta, "providers")
        for name in rnd.providers:
            _text(provs, "provider", name)
    # resume point and provider-reported totals, for "load more"
    if rnd.cursors or rnd.totals:
        cont = ET.SubElement(meta, "continuation")
        for name, cur in rnd.cursors.items():
            _text(cont, "cursor", cur, provider=name)
        for name, tot in rnd.totals.items():
            _text(cont, "total", tot, provider=name)
    if rnd.notes:
        _text(meta, "notes", rnd.notes)

    papers = ET.SubElement(root, "papers")
    for paper in rnd.papers:
        node = ET.SubElement(papers, "paper", {"id": paper.id})
        _text(node, "title", paper.title)

        authors = ET.SubElement(node, "authors", {"count": str(len(paper.authors))})
        for name in paper.authors:
            _text(authors, "author", name)

        _text(node, "publisher", paper.publisher)
        _text(node, "venue", paper.venue)
        _text(node, "year", paper.year)
        _text(node, "doi", paper.doi)
        _text(node, "abstract", paper.abstract, source=paper.abstract_source or None)
        if paper.cited_by is not None:
            _text(node, "citedBy", paper.cited_by)

        sources = ET.SubElement(node, "sources")
        for name in paper.sources:
            _text(sources, "source", name)

        links = ET.SubElement(node, "links")
        for rel, href in paper.links.items():
            _text(links, "link", href, rel=rel)

    path = round_path(session_id, rnd.number)
    path.write_bytes(_pretty(root))
    write_session_index(session_id)
    return path


def write_session_index(session_id: str) -> Path:
    directory = session_dir(session_id)
    root = ET.Element("scholarSession", {"id": session_id})
    _text(root, "updatedAt", now_iso())
    rounds = ET.SubElement(root, "rounds")
    for path in sorted(directory.glob("round_*.xml")):
        rnd = read_round(path)
        if rnd is None:
            continue
        node = ET.SubElement(rounds, "round", {"number": str(rnd.number)})
        _text(node, "label", rnd.label)
        _text(node, "query", rnd.query)
        _text(node, "scope", rnd.scope)
        _text(node, "resultCount", len(rnd.papers))
        _text(node, "file", path.name)
    out = directory / "session.xml"
    out.write_bytes(_pretty(root))
    return out


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def read_round(path: Path) -> Round | None:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    meta = root.find("meta")
    if meta is None:
        return None

    def m(tag: str, default: str = "") -> str:
        el = meta.find(tag)
        return (el.text or default) if el is not None else default

    period = meta.find("period")
    year_from = year_to = None
    if period is not None:
        year_from = _int_or_none((period.findtext("from") or "").strip())
        year_to = _int_or_none((period.findtext("to") or "").strip())

    rnd = Round(
        number=_int_or_none(m("round")) or 1,
        query=m("query"),
        scope=m("scope", "all"),
        year_from=year_from,
        year_to=year_to,
        source_round=_int_or_none(m("sourceRound")) or 0,
        created_at=m("createdAt"),
        notes=m("notes"),
        label=m("label"),
        providers=[(e.text or "").strip() for e in meta.findall("./providers/provider")],
        cursors={
            e.get("provider", ""): (e.text or "").strip()
            for e in meta.findall("./continuation/cursor")
            if e.get("provider")
        },
        totals={
            e.get("provider", ""): _int_or_none((e.text or "").strip()) or 0
            for e in meta.findall("./continuation/total")
            if e.get("provider")
        },
    )

    for node in root.findall("./papers/paper"):
        abstract_el = node.find("abstract")
        rnd.papers.append(
            Paper(
                id=node.get("id", ""),
                title=(node.findtext("title") or "").strip(),
                authors=[(a.text or "").strip() for a in node.findall("./authors/author")],
                publisher=(node.findtext("publisher") or "").strip(),
                venue=(node.findtext("venue") or "").strip(),
                year=_int_or_none((node.findtext("year") or "").strip()),
                doi=(node.findtext("doi") or "").strip(),
                sources=[(e.text or "").strip() for e in node.findall("./sources/source")],
                abstract=(abstract_el.text or "").strip() if abstract_el is not None else "",
                abstract_source=abstract_el.get("source", "") if abstract_el is not None else "",
                links={
                    link.get("rel", "link"): (link.text or "").strip()
                    for link in node.findall("./links/link")
                },
                cited_by=_int_or_none((node.findtext("citedBy") or "").strip()),
            )
        )
    return rnd


def read_session(session_id: str) -> Session | None:
    directory = session_dir(session_id)
    if not directory.is_dir():
        return None
    session = Session(id=session_id)
    for path in sorted(directory.glob("round_*.xml")):
        if not _ROUND_RE.match(path.name):
            continue
        rnd = read_round(path)
        if rnd is not None:
            session.rounds.append(rnd)
    session.rounds.sort(key=lambda r: r.number)
    session.created_at = session.rounds[0].created_at if session.rounds else now_iso()
    return session if session.rounds else None


def list_sessions() -> list[dict]:
    ensure_dirs()
    out = []
    for directory in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        session = read_session(directory.name)
        if session is None:
            continue
        first = session.rounds[0] if session.rounds else None
        out.append(
            {
                "id": session.id,
                "created_at": session.created_at,
                "rounds": len(session.rounds),
                "query": first.query if first else "",
                "label": (first.label if first and first.label else (first.query if first else "")),
                "total": len(session.rounds[-1].papers) if session.rounds else 0,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Deletion (spec 7.3.1)
# --------------------------------------------------------------------------- #
def delete_rounds_from(session_id: str, number: int) -> list[str]:
    """Delete the XML for round `number` through the last round.

    Round n+1 onward was derived from round n, so it must go too or the
    session becomes inconsistent.
    """
    directory = session_dir(session_id)
    removed = []
    if not directory.is_dir():
        return removed
    for path in sorted(directory.glob("round_*.xml")):
        m = _ROUND_RE.match(path.name)
        if m and int(m.group(1)) >= number:
            path.unlink(missing_ok=True)
            removed.append(path.name)

    if not any(directory.glob("round_*.xml")):
        shutil.rmtree(directory, ignore_errors=True)
    else:
        write_session_index(session_id)
    return removed


def delete_session(session_id: str) -> bool:
    directory = session_dir(session_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
        return True
    return False
