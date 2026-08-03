"""Domain models."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Local re-search scope (spec 5.2)
SCOPE_TITLE = "title"
SCOPE_TITLE_ABSTRACT = "title_abstract"
SCOPE_ALL = "all"  # the first web search targets the whole record (spec 2.3)


_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_doi(doi: str) -> str:
    """Normalize to bare '10.xxxx/yyy' — providers prefix DOIs differently."""
    doi = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip()


def normalize_title(title: str) -> str:
    """Title with punctuation/case/whitespace differences stripped, for cross-provider dedup."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", (title or "").lower())).strip()


@dataclass
class Paper:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    venue: str = ""              # journal / conference
    year: int | None = None
    abstract: str = ""
    abstract_source: str = ""    # snippet | fulltext | api
    links: dict[str, str] = field(default_factory=dict)
    cited_by: int | None = None
    doi: str = ""
    sources: list[str] = field(default_factory=list)  # openalex | gscholar
    id: str = ""

    def __post_init__(self) -> None:
        self.doi = normalize_doi(self.doi)
        if not self.id:
            self.id = self.make_id()

    def make_id(self) -> str:
        """Derive an ID that matches across providers for the same paper.

        DOI is the most reliable identifier when present. Otherwise fall back to
        normalized title + year (links vary by provider, so they're excluded).
        """
        key = f"doi:{self.doi}" if self.doi else f"{normalize_title(self.title)}|{self.year}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    # -- text fields local search looks at ---------------------------------- #
    @property
    def authors_text(self) -> str:
        return ", ".join(self.authors)

    @property
    def source_text(self) -> str:
        return " ".join(x for x in (self.venue, self.publisher) if x)

    def scope_text(self, scope: str) -> str:
        if scope == SCOPE_TITLE:
            return self.title
        if scope == SCOPE_TITLE_ABSTRACT:
            return f"{self.title}\n{self.abstract}"
        # SCOPE_ALL — the whole record
        return "\n".join(
            x for x in (self.title, self.authors_text, self.source_text, self.abstract) if x
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["authors_text"] = self.authors_text
        return d


@dataclass
class Round:
    """One search pass. Round 1 is a provider search; round 2+ is a local filter over the prior round."""

    number: int
    query: str
    scope: str = SCOPE_ALL
    year_from: int | None = None
    year_to: int | None = None
    source_round: int = 0        # 0 = fetched from providers, n = filtered from round n
    created_at: str = ""
    papers: list[Paper] = field(default_factory=list)
    notes: str = ""              # warnings: blocked, partial fetch, etc.
    providers: list[str] = field(default_factory=list)  # providers used in round 1
    # remembers each provider's next page position and total count, for "load more"
    cursors: dict[str, str] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    label: str = ""              # natural-language session name

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "query": self.query,
            "scope": self.scope,
            "year_from": self.year_from,
            "year_to": self.year_to,
            "source_round": self.source_round,
            "created_at": self.created_at,
            "notes": self.notes,
            "providers": self.providers,
            "cursors": self.cursors,
            "totals": self.totals,
            "label": self.label,
            "available": max(sum(self.totals.values()) if self.totals else 0, len(self.papers)),
            "has_more": bool(self.cursors),
            "count": len(self.papers),
            "papers": [p.to_dict() for p in self.papers],
        }


@dataclass
class Session:
    id: str
    created_at: str = ""
    rounds: list[Round] = field(default_factory=list)

    @property
    def latest(self) -> Round | None:
        return self.rounds[-1] if self.rounds else None

    def to_dict(self, with_papers: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "rounds": [
                r.to_dict() if with_papers else {**r.to_dict(), "papers": []}
                for r in self.rounds
            ],
        }


def slugify(text: str, limit: int = 40) -> str:
    text = re.sub(r'[":\\/*?<>|]+', " ", text)
    text = re.sub(r"\s+", "-", text.strip())
    return (text[:limit] or "search").strip("-")
