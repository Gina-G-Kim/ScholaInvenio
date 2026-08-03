"""Search query -> human-readable session name.

Spec: instead of showing the raw keyword syntax, join words, author, title,
venue, and year with commas into something that reads naturally. Double
quotes and the minus sign change meaning (phrase / exclusion), so they are
kept as-is in the name.

  author:PJ author:Hayes "naive physics" -survey  1990~2020
    -> author PJ Hayes, words "naive physics" -survey, 1990~2020
"""

from __future__ import annotations

from .query import Node, OrGroup, Term, parse_query

_FIELD_LABEL = {
    "author": "author",
    "intitle": "title",
    "source": "venue",
}
# Characters that are illegal or confusing in a filename.
# Double quotes stay in the session *name* (<label>) but are stripped from the
# directory name — legal on Linux, but breaks on Windows and is awkward in a shell.
_UNSAFE = '/\\:*?<>|"\n\r\t'


def _term_text(term: Term) -> str:
    """One search term, rendered back out. Quotes and minus carry meaning, so keep them."""
    body = f'"{term.value}"' if term.phrase else term.value
    return f"-{body}" if term.negated else body


def describe_query(
    query: str, year_from: int | None = None, year_to: int | None = None
) -> str:
    """Turn a query into a natural-language name, grouped by field and comma-joined."""
    nodes = parse_query(query or "")

    grouped: dict[str, list[str]] = {"author": [], "intitle": [], "source": []}
    words: list[str] = []

    for node in nodes:
        if isinstance(node, OrGroup):
            # read a union as "or"
            parts = [_term_text(t) for t in node.terms]
            fields = {t.field for t in node.terms if t.field}
            target = fields.pop() if len(fields) == 1 else None
            text = " or ".join(parts)
            if target in grouped:
                grouped[target].append(text)
            else:
                words.append(text)
            continue

        if node.field in grouped:
            grouped[node.field].append(_term_text(node))
        else:
            words.append(_term_text(node))

    chunks: list[str] = []
    for field in ("author", "intitle", "source"):
        if grouped[field]:
            chunks.append(f"{_FIELD_LABEL[field]} {' '.join(grouped[field])}")
    if words:
        chunks.append(f"words {' '.join(words)}")

    if year_from or year_to:
        chunks.append(f"{year_from or ''}~{year_to or ''}")

    return ", ".join(chunks) if chunks else "search"


def safe_dirname(text: str, limit: int = 70) -> str:
    """Turn a session name into something usable as a directory name.

    Minus signs carry meaning, so they're kept. Path-meaningful characters and
    double quotes are stripped. The name shown to the user is the XML <label>,
    which keeps the quotes intact.
    """
    out = "".join(" " if ch in _UNSAFE else ch for ch in text)
    out = " ".join(out.split())          # collapse whitespace
    out = out.replace(" ", "_")
    return out[:limit].strip("_.") or "search"
