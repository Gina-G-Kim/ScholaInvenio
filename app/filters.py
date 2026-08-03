"""Local re-search over a collected list (spec 5) and the date-range filter (spec 2.4).

Supports the exact same keyword syntax as the initial search (5.1), plus a
"title only" / "title+abstract only" scope selection (5.2).
"""

from __future__ import annotations

import re

from .models import Paper, SCOPE_ALL, SCOPE_TITLE, SCOPE_TITLE_ABSTRACT
from .query import Node, OrGroup, Term, parse_query


def _to_regex(value: str) -> re.Pattern | None:
    """Turn a search term into a regex. '*' is a wildcard.

    - '*' inside a word  -> any characters there (learn* == learning)
    - standalone '*'     -> a whole word         ("a * c" == a foo c)
    """
    words = value.split()
    if not words:
        return None

    chunks: list[str] = []
    for word in words:
        if word == "*":
            chunks.append(r"\S+")
        elif "*" in word:
            chunks.append("".join(r"[\w\-]*" if ch == "*" else re.escape(ch) for ch in word))
        else:
            chunks.append(re.escape(word))

    core = r"\s+".join(chunks)
    return re.compile(rf"(?<!\w){core}(?!\w)", re.IGNORECASE)


def _initials_of(name: str) -> str:
    """'Patrick J. Hayes' -> 'PJH'."""
    return "".join(w[0].upper() for w in re.findall(r"[A-Za-z]+", name))


class CompiledTerm:
    __slots__ = ("field", "negated", "pattern", "raw", "initials")

    def __init__(self, term: Term) -> None:
        self.field = term.field
        self.negated = term.negated
        self.raw = term.value
        self.pattern = _to_regex(term.value)
        # Author initials like 'PJ' are spelled out differently by different
        # providers — Scholar gives 'PJ Hayes', OpenAlex gives 'Patrick J. Hayes'.
        # So an initials-looking token is also compared against each name's
        # first letters (this only broadens the match, never narrows it).
        self.initials = ""
        if term.field == "author" and not term.phrase:
            bare = term.value.replace(".", "")
            if bare.isalpha() and len(bare) <= 3:
                self.initials = bare.upper()

    def _haystack(self, paper: Paper, scope: str) -> str:
        # A field-qualified search looks at that field regardless of the scope checkbox.
        if self.field == "author":
            return paper.authors_text
        if self.field == "intitle":
            return paper.title
        if self.field == "source":
            return paper.source_text
        return paper.scope_text(scope)

    def hit(self, paper: Paper, scope: str) -> bool:
        if self.pattern is None:
            return True
        haystack = self._haystack(paper, scope)
        if self.pattern.search(haystack):
            return True
        if self.initials:
            # Compare against each author's own initials individually.
            for name in re.split(r"[,;]", haystack):
                if self.initials in _initials_of(name):
                    return True
        return False

    def matches(self, paper: Paper, scope: str) -> bool:
        found = self.hit(paper, scope)
        return (not found) if self.negated else found


class CompiledQuery:
    """Clauses ANDed together. Each clause is a single Term or an OR group."""

    def __init__(self, nodes: list[Node]) -> None:
        self.clauses: list[list[CompiledTerm]] = []
        for node in nodes:
            if isinstance(node, OrGroup):
                self.clauses.append([CompiledTerm(t) for t in node.terms])
            else:
                self.clauses.append([CompiledTerm(node)])

    @property
    def empty(self) -> bool:
        return not self.clauses

    def matches(self, paper: Paper, scope: str) -> bool:
        for clause in self.clauses:
            if len(clause) == 1:
                if not clause[0].matches(paper, scope):
                    return False
                continue

            # OR group: every negated term must pass, and at least one positive
            # term must match.
            positives = [c for c in clause if not c.negated]
            negatives = [c for c in clause if c.negated]
            if any(not c.matches(paper, scope) for c in negatives):
                return False
            if positives and not any(c.matches(paper, scope) for c in positives):
                return False
        return True


def compile_query(text: str) -> CompiledQuery:
    return CompiledQuery(parse_query(text))


def in_year_range(paper: Paper, year_from: int | None, year_to: int | None) -> bool:
    """Spec 2.4 — Scholar has no date-range syntax, so filter locally.

    A paper with no readable year is excluded once a range is given (not
    because a miss is safer than a false positive, but to honor the range the
    user explicitly asked for).
    """
    if year_from is None and year_to is None:
        return True
    if paper.year is None:
        return False
    if year_from is not None and paper.year < year_from:
        return False
    if year_to is not None and paper.year > year_to:
        return False
    return True


def apply_filter(
    papers: list[Paper],
    query: str = "",
    scope: str = SCOPE_TITLE_ABSTRACT,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[Paper]:
    if scope not in (SCOPE_TITLE, SCOPE_TITLE_ABSTRACT, SCOPE_ALL):
        scope = SCOPE_TITLE_ABSTRACT
    compiled = compile_query(query)
    out = []
    for paper in papers:
        if not in_year_range(paper, year_from, year_to):
            continue
        if not compiled.empty and not compiled.matches(paper, scope):
            continue
        out.append(paper)
    return out
