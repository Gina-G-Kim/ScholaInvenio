"""Google Scholar search-syntax builder / parser.

Covers spec sections 2, 2.1, 2.4.

Supported syntax
  - exact phrase : "..."
  - union        : A OR B
  - exclude      : -term
  - wildcard     : *
  - author       : author:<name>
  - title        : intitle:<text>
  - venue/source : source:<text>
  - date range   : Scholar has no syntax for this, so it is kept out of the
                   query string and stored as separate metadata instead (2.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

FIELDS = ("author", "intitle", "source")

# Parse author:/intitle:/source: as well as the all* variants Scholar understands.
_FIELD_RE = re.compile(r"(allintitle|allinauthor|intitle|author|source)\s*:", re.I)
_FIELD_ALIAS = {
    "allintitle": "intitle",
    "allinauthor": "author",
}


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
@dataclass
class Term:
    """A single search term. May carry a field qualifier (author: etc.) and negation (-)."""

    value: str
    field: str | None = None
    negated: bool = False
    phrase: bool = False  # was it wrapped in double quotes

    def render(self) -> str:
        body = f'"{self.value}"' if self.phrase else self.value
        if self.field:
            body = f"{self.field}:{body}"
        return f"-{body}" if self.negated else body


@dataclass
class OrGroup:
    """A OR B OR C — union inside the group, intersection with everything else."""

    terms: list[Term] = field(default_factory=list)

    def render(self) -> str:
        return " OR ".join(t.render() for t in self.terms)


Node = Term | OrGroup


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _scan(text: str) -> Iterator[Term]:
    """Split a query string into Terms, respecting quotes.

    'OR' is emitted as a marker Term whose value is "OR"; the caller groups it.
    """
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break

        negated = False
        if text[i] == "-" and i + 1 < n and not text[i + 1].isspace():
            negated = True
            i += 1

        fname: str | None = None
        m = _FIELD_RE.match(text, i)
        if m:
            fname = _FIELD_ALIAS.get(m.group(1).lower(), m.group(1).lower())
            i = m.end()

        if i < n and text[i] == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                i += 1
            value = text[start:i]
            if i < n:
                i += 1
            phrase = True
        else:
            start = i
            while i < n and not text[i].isspace():
                i += 1
            value = text[start:i]
            phrase = False

        if not value:
            continue
        yield Term(value=value, field=fname, negated=negated, phrase=phrase)


def parse_query(text: str) -> list[Node]:
    """Query string -> a list of Nodes, ANDed together."""
    raw = list(_scan(text or ""))
    nodes: list[Node] = []
    idx = 0
    while idx < len(raw):
        tok = raw[idx]

        # 'OR' marker: fold the previous node and the next token into a union.
        if not tok.field and not tok.phrase and tok.value.upper() == "OR" and not tok.negated:
            if nodes and idx + 1 < len(raw):
                prev = nodes.pop()
                group = prev if isinstance(prev, OrGroup) else OrGroup([prev])
                group.terms.append(raw[idx + 1])
                nodes.append(group)
                idx += 2
                continue
            idx += 1  # drop a stray OR with nothing on one side
            continue

        nodes.append(tok)
        idx += 1
    return nodes


def render_query(nodes: list[Node]) -> str:
    return " ".join(node.render() for node in nodes)


def split_source_terms(text: str) -> tuple[str, str]:
    """Pull source: terms out of a query, e.g. because a provider (Google
    Scholar) has no venue operator at all: unlike author:/intitle:, which are
    genuine Scholar syntax passed through natively, 'source:' is this tool's
    own invention and means nothing to Scholar's search box. Returns (the
    query with source: terms removed, the source: terms alone) so the caller
    can send the first to the provider and re-check the second locally
    (filters.py already applies a term's field, phrase and negation correctly
    regardless of which provider produced the results).
    """
    remaining: list[Node] = []
    source_only: list[str] = []
    for node in parse_query(text):
        if isinstance(node, Term) and node.field == "source":
            source_only.append(node.render())
            continue
        if isinstance(node, OrGroup) and any(t.field == "source" for t in node.terms):
            # A source: term mixed into an OR group changes the group's
            # semantics if split apart, so the whole group is kept together
            # and re-checked locally instead.
            source_only.append(node.render())
            continue
        remaining.append(node)
    return render_query(remaining), " ".join(source_only)


# --------------------------------------------------------------------------- #
# Building (GUI keyword builder -> query string)
# --------------------------------------------------------------------------- #
def _split_units(text: str) -> list[tuple[str, bool]]:
    """Split on whitespace but keep "..." chunks intact.

    Returns: [(value, was_quoted), ...]
    """
    units: list[tuple[str, bool]] = []
    for term in _scan(text or ""):
        units.append((term.value, term.phrase))
    return units


def prefix_each_word(text: str, field_name: str) -> list[str]:
    """Spec 2.1 — repeat the field syntax for every word in a field search.

    'PJ Hayes'         -> ['author:PJ', 'author:Hayes']
    '"PJ Hayes" Cohen' -> ['author:"PJ Hayes"', 'author:Cohen']
    """
    out = []
    for value, quoted in _split_units(text):
        if value.upper() == "OR":  # pass OR through unchanged inside a field
            out.append("OR")
            continue
        body = f'"{value}"' if quoted or " " in value else value
        out.append(f"{field_name}:{body}")
    return out


def _quote_if_needed(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"' if " " in value else value


@dataclass
class QuerySpec:
    """One set of GUI keyword inputs. Every field is optional."""

    all_words: str = ""          # whitespace = AND (spec 2.2)
    exact_phrase: str = ""       # "..." — newline/semicolon separates multiple
    or_terms: str = ""           # comma-separated alternatives -> A OR B
    exclude: str = ""            # -term
    wildcard: str = ""           # pattern containing *
    author: str = ""             # author:
    intitle: str = ""            # intitle:
    source: str = ""             # source:
    year_from: int | None = None  # kept out of the query string (2.4)
    year_to: int | None = None

    def build(self) -> str:
        parts: list[str] = []

        if self.all_words.strip():
            for value, quoted in _split_units(self.all_words):
                parts.append(f'"{value}"' if quoted else value)

        for chunk in re.split(r"[\n;]+", self.exact_phrase or ""):
            chunk = chunk.strip().strip('"').strip()
            if chunk:
                parts.append(f'"{chunk}"')

        alternatives = [c.strip() for c in re.split(r"[,\n]+", self.or_terms or "") if c.strip()]
        if len(alternatives) == 1:
            parts.append(_quote_if_needed(alternatives[0]))
        elif alternatives:
            parts.append(" OR ".join(_quote_if_needed(a) for a in alternatives))

        for chunk in re.split(r"[,\n]+", self.exclude or ""):
            chunk = chunk.strip().lstrip("-").strip()
            if chunk:
                parts.append(f"-{_quote_if_needed(chunk)}")

        for value, quoted in _split_units(self.wildcard):
            parts.append(f'"{value}"' if quoted else value)

        parts += prefix_each_word(self.author, "author")
        parts += prefix_each_word(self.intitle, "intitle")
        parts += prefix_each_word(self.source, "source")

        return " ".join(p for p in parts if p)


def describe(spec: QuerySpec) -> str:
    """Human-readable summary including the date range (used in column headers)."""
    q = spec.build()
    if spec.year_from or spec.year_to:
        lo = spec.year_from or ""
        hi = spec.year_to or ""
        q = f"{q}  [{lo}~{hi}]".strip()
    return q
