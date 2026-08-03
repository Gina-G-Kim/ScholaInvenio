"""Does venue A's title mean the same venue as name B?

Shared by every provider that has to decide whether a venue record it found
by fuzzy/prefix text search is actually the one the user asked for, rather
than a related-but-distinct venue that happens to share words (a sister
conference, a workshop co-located with something else, a generic survey
venue). Used by openalex.py (matching a proceedings' front-matter title
against the requested venue name) and dblp.py (matching a canonical DBLP
venue string the same way).
"""

from __future__ import annotations

import re

_LEADING_YEAR_RE = re.compile(r"^(19|20)\d{2}\s*[:\-–—]?\s*")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE)
_STOPWORDS = {"the", "a", "an", "on", "of", "for", "in", "and", "proceedings"}
# A venue's own workshop track is still the same venue for this purpose; any
# other extra word ('European', 'Companion', ...) means it's a related but
# distinct venue instead (confirmed: IEEE Symposium on Security and Privacy
# vs IEEE *European* Symposium on Security and Privacy are separately
# organized conferences that both happen to match a loose text search).
ALLOWED_EXTRA_TOKENS = {"workshop", "workshops"}


def clean_venue_title(title: str) -> str:
    """Strip a leading year and a trailing parenthetical acronym."""
    t = _LEADING_YEAR_RE.sub("", title or "")
    t = _TRAILING_PAREN_RE.sub("", t)
    return t.strip()


def venue_tokens(title: str) -> set[str]:
    t = _ORDINAL_RE.sub("", clean_venue_title(title))
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in _STOPWORDS}


def looks_like_same_venue(candidate_title: str, target_name: str) -> bool:
    """Order- and noise-tolerant match: a title can reorder words
    ('International Requirements Engineering Conference' vs the target's
    'International Conference on Requirements Engineering') and carry an
    edition ordinal ('32nd') that a strict substring or word-order check
    would choke on, so this compares the meaningful token sets instead. Any
    extra word beyond the allowed set means a related-but-different venue,
    not this one, so it takes an exact token match rather than a subset.
    """
    target_tokens = venue_tokens(target_name)
    if not target_tokens:
        return False
    candidate_tokens = venue_tokens(candidate_title)
    if not target_tokens <= candidate_tokens:
        return False
    return candidate_tokens - target_tokens <= ALLOWED_EXTRA_TOKENS
