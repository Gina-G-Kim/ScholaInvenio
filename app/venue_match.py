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
_TRAILING_YEAR_RE = re.compile(r"\s*,?\s*(19|20)\d{2}\s*$")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE)
# The major sponsoring societies are noise for this comparison, symmetrically
# on both sides: the same venue is routinely referred to both with and
# without one, in either direction, and which form a user (or DBLP, or
# OpenAlex's own /sources display_name) happens to use doesn't mean a
# different event. Confirmed live, both directions:
#   - "The Web Conference" (target) vs "Proceedings of the ACM Web Conference
#     2024" (candidate) -- the extra 'acm' made every yearly unlinked edition
#     fail, so the venue was reported as not found at all.
#   - "IEEE International Requirements Engineering Conference" (target) vs
#     OpenAlex's own splinter record "International Conference on
#     Requirements Engineering" (candidate, missing 'ieee') -- the target
#     asking for a token this candidate didn't happen to have made a second,
#     genuinely-the-same-venue record for the same conference fail too.
# This is a small, fixed set of umbrella *organizations* that sponsor
# thousands of otherwise-unrelated venues between them, not a list of
# conferences -- adding a venue never means editing this.
_STOPWORDS = {"the", "a", "an", "on", "of", "for", "in", "and", "proceedings", "acm", "ieee", "usenix"}
# A venue's own workshop track is still the same venue for this purpose; any
# other extra word ('European', 'Companion', ...) means it's a related but
# distinct venue instead (confirmed: IEEE Symposium on Security and Privacy
# vs IEEE *European* Symposium on Security and Privacy are separately
# organized conferences that both happen to match a loose text search). This
# one only ever needs to be checked in one direction -- extra in the
# candidate -- since a target that explicitly asks for the workshop track is
# specifically asking for it, not something a plain conference match should
# silently satisfy instead.
ALLOWED_EXTRA_TOKENS = {"workshop", "workshops"}


def clean_venue_title(title: str) -> str:
    """Strip a year (leading or trailing) and a trailing parenthetical acronym.

    Different publishers put the edition year in different places -- IEEE's
    convention leads with it ('2024 IEEE ... Conference'), ACM's trails it
    ('Proceedings of the ACM Web Conference 2024'). Only the leading case was
    handled here; a trailing year survived into venue_tokens() as a stray
    extra token, which made looks_like_same_venue reject every year's own
    edition of any venue named ACM's way (confirmed live: none of "ACM Web
    Conference"'s yearly unlinked editions matched, each rejected over its
    own trailing year). Strip both, repeating in case removing one exposes
    the other (a trailing paren after a trailing year, or vice versa).
    """
    t = _LEADING_YEAR_RE.sub("", title or "")
    while True:
        stripped = _TRAILING_YEAR_RE.sub("", _TRAILING_PAREN_RE.sub("", t))
        if stripped == t:
            break
        t = stripped
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
