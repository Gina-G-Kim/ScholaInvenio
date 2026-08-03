"""FastAPI app — serves the GUI static files and the search API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import xmlstore
from .filters import apply_filter
from .merge import merge as merge_results
from .models import Round, SCOPE_ALL, SCOPE_TITLE, SCOPE_TITLE_ABSTRACT
from .naming import describe_query
from .providers import (
    DEFAULT_PROVIDERS,
    MAX_RESULTS,
    PAGE_BATCH,
    PROVIDER_LABELS,
    gather_providers,
)
from .query import QuerySpec, describe

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    xmlstore.ensure_dirs()
    yield


app = FastAPI(title="ScholaInvenio", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Request schemas
# --------------------------------------------------------------------------- #
class SpecIn(BaseModel):
    all_words: str = ""
    exact_phrase: str = ""
    or_terms: str = ""
    exclude: str = ""
    wildcard: str = ""
    author: str = ""
    intitle: str = ""
    source: str = ""
    year_from: int | None = None
    year_to: int | None = None

    def to_spec(self) -> QuerySpec:
        return QuerySpec(**self.model_dump())


class SearchIn(BaseModel):
    query: str = ""
    year_from: int | None = None
    year_to: int | None = None
    # Default to the ceiling. The official API returns 200-1000 per page, so
    # this doesn't multiply the request count much. Only Google Scholar is
    # slow and can get blocked partway through, in which case whatever was
    # collected so far is kept.
    max_results: int = Field(default=MAX_RESULTS, ge=1, le=MAX_RESULTS)
    fetch_abstracts: bool = False
    # Defaults to the official API. Google Scholar is opt-in.
    providers: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDERS))


class RefineIn(BaseModel):
    query: str = ""
    scope: str = SCOPE_TITLE_ABSTRACT
    year_from: int | None = None
    year_to: int | None = None


# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "backend": os.environ.get("SCHOLAR_BACKEND", "html")}


@app.get("/api/config")
def get_config() -> dict:
    """Lets the GUI pull its defaults from the server, so the ceiling doesn't drift apart in two places."""
    from . import openalex
    return {
        "max_results": MAX_RESULTS,
        "page_batch": PAGE_BATCH,
        "default_providers": list(DEFAULT_PROVIDERS),
        "providers": PROVIDER_LABELS,
        "openalex_has_key": openalex.has_api_key(),
    }


@app.post("/api/describe")
def describe_name(body: dict) -> dict:
    """Preview the natural-language session name for a query."""
    return {
        "label": describe_query(
            str(body.get("query") or ""), body.get("year_from"), body.get("year_to")
        )
    }


# --------------------------------------------------------------------------- #
# Keyword builder (spec 1.1, 2, 2.1)
# --------------------------------------------------------------------------- #
@app.post("/api/build-query")
def build_query(spec_in: SpecIn) -> dict:
    """GUI input fields -> Scholar keyword syntax. The server owns all syntax assembly."""
    spec = spec_in.to_spec()
    return {
        "query": spec.build(),
        "display": describe(spec),
        "year_from": spec.year_from,
        "year_to": spec.year_to,
    }


# --------------------------------------------------------------------------- #
# Sessions / rounds
# --------------------------------------------------------------------------- #
def _checked(session_id: str) -> str:
    """Confirm a session id doesn't point outside the storage directory."""
    try:
        xmlstore.session_dir(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session id.") from exc
    return session_id


@app.get("/api/sessions")
def get_sessions() -> dict:
    return {"sessions": xmlstore.list_sessions()}


@app.get("/api/session/{session_id}")
def get_session(session_id: str) -> dict:
    session = xmlstore.read_session(_checked(session_id))
    if session is None:
        raise HTTPException(404, "Session not found.")
    return session.to_dict()


@app.delete("/api/session/{session_id}")
def drop_session(session_id: str) -> dict:
    return {"deleted": xmlstore.delete_session(_checked(session_id))}


@app.post("/api/search")
async def run_search(body: SearchIn) -> JSONResponse:
    """Round 1 — query the selected providers concurrently, merge, and save as round_01.xml."""
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Enter a search query.")

    providers = [p for p in body.providers if p in PROVIDER_LABELS]
    if not providers:
        raise HTTPException(400, "Select at least one data provider.")

    results, notes, failed, cursors, totals = await gather_providers(
        providers=providers,
        query=query,
        max_results=body.max_results,
        year_from=body.year_from,
        year_to=body.year_to,
        fetch_abstracts=body.fetch_abstracts,
    )

    # A provider that returned 0 results is different from one that actually failed.
    if len(failed) == len(providers):
        raise HTTPException(502, " / ".join(notes))

    per_source = {n: len(v) for n, v in results.items()}
    papers = merge_results(results)
    raw_total = sum(per_source.values())

    # Keyword matching was already finished by each provider, natively or via
    # a residual check. Re-running the full query here would re-judge results
    # a provider's full-text index already matched, using our thinner
    # metadata, and drop perfectly good papers — so it isn't done.
    #
    # Date range is different: year is a plain field on the record, so
    # re-checking it never causes a false drop, and the user's requested
    # range must hold even if a provider ignored it.
    if body.year_from is not None or body.year_to is not None:
        before = len(papers)
        papers = apply_filter(papers, "", SCOPE_ALL, body.year_from, body.year_to)
        if len(papers) != before:
            notes.append(f"date range narrowed {before} results down to {len(papers)}.")

    papers = papers[: body.max_results]

    summary = ", ".join(f"{PROVIDER_LABELS.get(n, n)} {c}" for n, c in per_source.items())
    notes_text = " ".join([f"Fetched {summary} -> {len(papers)} after dedup."] + notes).strip()

    label = describe_query(query, body.year_from, body.year_to)
    session_id = xmlstore.new_session_id(label)
    rnd = Round(
        number=1,
        query=query,
        scope=SCOPE_ALL,
        year_from=body.year_from,
        year_to=body.year_to,
        source_round=0,
        created_at=xmlstore.now_iso(),
        papers=papers,
        notes=notes_text,
        providers=providers,
        cursors=cursors,
        totals=totals,
        label=label,
    )
    xmlstore.write_round(session_id, rnd)
    return JSONResponse({
        "session_id": session_id,
        "round": rnd.to_dict(),
        "per_source": per_source,
        "raw_total": raw_total,
    })


@app.post("/api/session/{session_id}/round/{number}/more")
async def fetch_more(session_id: str, number: int) -> JSONResponse:
    """Fetch another batch and **append it to the same round's XML**.

    Resumes from the cursor each provider handed back, so results already
    seen aren't fetched again.
    """
    session = xmlstore.read_session(_checked(session_id))
    if session is None:
        raise HTTPException(404, "Session not found.")
    rnd = next((r for r in session.rounds if r.number == number), None)
    if rnd is None:
        raise HTTPException(404, "That round does not exist.")
    if rnd.source_round != 0:
        raise HTTPException(400, "Loading more only works on round 1 (the provider search).")
    if not rnd.cursors:
        raise HTTPException(400, "There is nothing more to fetch.")

    providers = [p for p in rnd.cursors if p in PROVIDER_LABELS]
    if not providers:
        raise HTTPException(400, "No provider can be resumed.")

    results, notes, failed, cursors, totals = await gather_providers(
        providers=providers,
        query=rnd.query,
        max_results=PAGE_BATCH,
        year_from=rnd.year_from,
        year_to=rnd.year_to,
        cursors=rnd.cursors,
    )
    if len(failed) == len(providers):
        raise HTTPException(502, " / ".join(notes))

    fetched = merge_results(results)
    if rnd.year_from is not None or rnd.year_to is not None:
        fetched = apply_filter(fetched, "", SCOPE_ALL, rnd.year_from, rnd.year_to)

    # merge with the existing list, deduping (a resumed cursor can still overlap)
    before = len(rnd.papers)
    known = {p.id for p in rnd.papers}
    added = [p for p in fetched if p.id not in known]
    rnd.papers = rnd.papers + added
    rnd.cursors = cursors
    if totals:
        rnd.totals.update(totals)
    added_note = f"Fetched {len(added)} more ({len(rnd.papers)} total)."
    rnd.notes = f"{rnd.notes} {added_note}".strip() if rnd.notes else added_note
    if notes:
        rnd.notes = f"{rnd.notes} " + " ".join(notes)

    # rounds derived from this one no longer reflect it, so they're stale.
    stale = [r.number for r in session.rounds if r.number > number]
    if stale:
        xmlstore.delete_rounds_from(session_id, number + 1)

    xmlstore.write_round(session_id, rnd)
    return JSONResponse({
        "session_id": session_id,
        "round": rnd.to_dict(),
        "added": len(added),
        "before": before,
        "dropped_rounds": stale,
    })


@app.post("/api/session/{session_id}/refine")
def refine(session_id: str, body: RefineIn) -> dict:
    """Round n (spec 5, 6) — filters the previous round's list, not the internet."""
    session = xmlstore.read_session(_checked(session_id))
    if session is None:
        raise HTTPException(404, "Session not found.")
    source = session.latest
    if source is None:
        raise HTTPException(400, "There is no prior round to filter.")

    scope = body.scope if body.scope in (SCOPE_TITLE, SCOPE_TITLE_ABSTRACT) else SCOPE_TITLE_ABSTRACT
    if not body.query.strip() and body.year_from is None and body.year_to is None:
        raise HTTPException(400, "Enter a keyword or a date range to refine by.")

    papers = apply_filter(source.papers, body.query, scope, body.year_from, body.year_to)
    rnd = Round(
        number=source.number + 1,
        query=body.query.strip(),
        scope=scope,
        year_from=body.year_from,
        year_to=body.year_to,
        source_round=source.number,
        created_at=xmlstore.now_iso(),
        papers=papers,
    )
    xmlstore.write_round(session_id, rnd)
    return {"session_id": session_id, "round": rnd.to_dict()}


@app.delete("/api/session/{session_id}/round/{number}")
def rollback(session_id: str, number: int) -> dict:
    """Spec 7.3 / 7.3.1 — delete that round's XML and roll back to the prior round."""
    if number < 1:
        raise HTTPException(400, "Invalid round number.")
    session = xmlstore.read_session(_checked(session_id))
    if session is None:
        raise HTTPException(404, "Session not found.")

    removed = xmlstore.delete_rounds_from(session_id, number)
    remaining = xmlstore.read_session(session_id)
    return {
        "removed": removed,
        "session": remaining.to_dict() if remaining else None,
    }


@app.get("/api/session/{session_id}/round/{number}/xml")
def download_round(session_id: str, number: int) -> FileResponse:
    path = xmlstore.round_path(_checked(session_id), number)
    if not path.is_file():
        raise HTTPException(404, "No such XML file.")
    return FileResponse(path, media_type="application/xml", filename=path.name)
