"""Translate Scholar keyword syntax into each provider's native query.

Users see exactly one syntax — the one in spec section 2. `query.parse_query`
turns it into a common AST, and this module carries that AST over into each
provider's own query language.

    Scholar syntax string
            |
        parse_query()
            v
      AST (Term/OrGroup)  <- common intermediate representation
            |
      +-----+--------------+
      v                    v
  OpenAlex          Google Scholar

Conditions a provider can't express natively (e.g. wildcards on OpenAlex) are
flagged as `residual` and re-checked locally after fetching, via `filters.py`.
That way the final result carries the same syntax semantics no matter which
provider produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .query import Node, OrGroup, Term, parse_query

# Tokens that look like initials, e.g. "PJ", "J", "A".
#
# Spec 2.1's "repeat the syntax per word" (PJ Hayes -> author:PJ author:Hayes)
# is a Google Scholar convention. Scholar tokenizes an author as 'PJ Hayes', so
# it lines up neatly, but OpenAlex stores 'Patrick J. Hayes', so 'PJ' matches
# nothing there. ANDing that in would zero out the whole result, so an
# initials-looking token is kept out of the native filter and only the
# surname token is sent. The initial is instead cross-checked locally against
# each name's own initials (see filters.py).
_INITIALS_RE = re.compile(r"^[A-Za-z]\.?([A-Za-z]\.?){0,2}$")


def is_initials(token: str) -> bool:
    """Does this token look like initials — no vowels, or just very short."""
    bare = token.replace(".", "")
    if len(bare) > 3 or not bare.isalpha():
        return False
    return bare.isupper() or len(bare) <= 2


def substantive_tokens(value: str) -> list[str]:
    """The tokens worth sending to search, with initials stripped out."""
    return [t for t in value.split() if not is_initials(t)]


@dataclass
class Translated:
    """Parameters for a provider, plus whatever condition it couldn't handle.

    `residual` is a fragment of Scholar syntax to be re-applied via
    `filters.py` after fetching. A condition the provider already handled
    natively must never end up here — re-checking it would re-judge results
    the provider's full-text index already matched, using our thinner
    metadata, and drop perfectly good papers.
    """

    params: dict[str, str] = field(default_factory=dict)
    residual: str = ""
    # OpenAlex has no filter for searching venue names as text. The name must
    # first be resolved to an ID via /sources, then filtered on
    # primary_location.source.id. That lookup needs a network call, so only
    # the name is passed along here; providers.py performs the lookup.
    source_lookup: str = ""
    # condition to fall back to locally if that lookup fails
    source_residual: str = ""


def _has_wildcard(term: Term) -> bool:
    return "*" in term.value


def _quote(term: Term) -> str:
    """Wrap in quotes if it's a phrase or contains whitespace."""
    value = term.value
    return f'"{value}"' if term.phrase or " " in value else value


# --------------------------------------------------------------------------- #
# OpenAlex
# --------------------------------------------------------------------------- #
# Only a limited set of fields on OpenAlex support a '.search' suffix; venue
# name is not one of them.
_OA_FIELD = {
    "author": "raw_author_name.search",
    "intitle": "title.search",
}
# Spec 5.2's scope selector -> OpenAlex full-text search field
OA_SCOPE_FILTER = {
    "title": "title.search",
    "title_abstract": "title_and_abstract.search",
}


def to_openalex(
    text: str,
    year_from: int | None = None,
    year_to: int | None = None,
    scope: str = "all",
) -> Translated:
    nodes = parse_query(text)
    free_terms: list[str] = []
    filters: list[str] = []
    residual: list[str] = []
    source_terms: list[str] = []      # source: tokens (joined into one venue name)
    source_residual: list[str] = []

    def render(term: Term) -> str:
        # OpenAlex full-text search doesn't support *. Strip it and send an
        # approximate query, then re-check that term exactly, locally.
        value = term.value.replace("*", " ").strip()
        return f'"{value}"' if term.phrase or " " in value else value

    for node in nodes:
        if isinstance(node, OrGroup):
            # if a field qualifier or wildcard is mixed into an OR group, check the whole thing locally
            if any(t.field or _has_wildcard(t) for t in node.terms):
                residual.append(node.render())
                continue
            parts = [r for r in (render(t) for t in node.terms) if r]
            if parts:
                free_terms.append("(" + " OR ".join(parts) + ")")
            continue

        term = node
        if term.field == "source":
            # Tokens split apart by spec 2.1 are really one venue name, unlike
            # author, so resolve them together as a single lookup.
            if term.negated or _has_wildcard(term):
                residual.append(term.render())
            else:
                source_terms.append(term.value)
                source_residual.append(term.render())
            continue

        if term.field:
            key = _OA_FIELD.get(term.field)
            if not key or term.negated or _has_wildcard(term):
                residual.append(term.render())
                continue
            value = term.value
            recheck_locally = term.phrase   # see note below
            if term.field == "author":
                usable = substantive_tokens(value)
                if not usable:
                    # nothing but initials — can't filter natively, defer to local check
                    residual.append(term.render())
                    continue
                value = " ".join(usable)
                if usable != value.split() or len(usable) != len(term.value.split()):
                    recheck_locally = True   # also verify the dropped initials locally
            filters.append(f"{key}:{value.replace(',', ' ')}")
            if recheck_locally:
                # OpenAlex's '.search' filter is a relevance match, not an exact
                # phrase — 'title.search:deep learning' can match a title with
                # those words apart, in either order. A quoted phrase promises
                # adjacency, which only the local regex check (filters.py)
                # actually enforces, so it re-verifies on top of the native
                # (approximate) filter rather than replacing it.
                residual.append(term.render())
            continue

        if _has_wildcard(term):
            # send an approximate query with the * stripped; verify exactly, locally
            approx = render(term)
            if approx:
                free_terms.append(f"NOT {approx}" if term.negated else approx)
            residual.append(term.render())
            continue

        rendered = render(term)
        if not rendered:
            continue
        free_terms.append(f"NOT {rendered}" if term.negated else rendered)

    scope_key = OA_SCOPE_FILTER.get(scope)
    params: dict[str, str] = {}
    if free_terms:
        joined = " AND ".join(free_terms)
        if scope_key:
            filters.append(f"{scope_key}:{joined}")
        else:
            params["search"] = joined

    # Spec 2.4 — date range is native here (no post-processing needed, unlike Scholar).
    if year_from is not None and year_to is not None:
        filters.append(f"publication_year:{year_from}-{year_to}")
    elif year_from is not None:
        filters.append(f"publication_year:>{year_from - 1}")
    elif year_to is not None:
        filters.append(f"publication_year:<{year_to + 1}")

    if filters:
        params["filter"] = ",".join(filters)
    return Translated(
        params=params,
        residual=" ".join(residual),
        source_lookup=" ".join(source_terms),
        source_residual=" ".join(source_residual),
    )


# --------------------------------------------------------------------------- #
# Google Scholar
# --------------------------------------------------------------------------- #
def to_gscholar(text: str) -> Translated:
    """Scholar is where this syntax comes from, so pass it through unchanged.

    Every operator is handled natively, so there's no residual — except date
    range, which Scholar has no syntax for; the caller filters that
    separately (spec 2.4).
    """
    return Translated(params={"q": text}, residual="")
