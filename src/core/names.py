"""Matching names of shows, teams and players that differ in case, punctuation, accents or script,
and finding the closest match for a name with a typo.

Both sides of a comparison go through normalize, so "Pokémon" matches "pokemon" and a name in
Cyrillic or Japanese is transliterated rather than dropped. The closest-match functions are only a
fallback for when nothing matches exactly, and tool text always says their result is a guess.
"""

import re
from collections.abc import Iterable
from typing import TypeVar

from anyascii import anyascii
from rapidfuzz import fuzz, process

# Checked on real titles and Valve's standings: typos score 83 or more, unrelated names under 80
# ("freiren" scores 77 against "friend", "xyzzy" 67 against "extazzy")
WORD_CUTOFF = 80
NAME_CUTOFF = 80

T = TypeVar("T")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", anyascii(text).lower()).strip()

def closest_by_words(name: str, candidates: Iterable[tuple[T, list[str]]]) -> T | None:
    """The candidate with a title in which every word of name has a close word (a typo or two
    each), best on average; for long titles of which the user types a few words."""
    words = normalize(name).split()
    best, best_score = None, 0.0
    for candidate, titles in candidates:
        for title in titles:
            title_words = normalize(title).split()
            if not words or not title_words:
                continue
            scores = [process.extractOne(word, title_words, scorer=fuzz.ratio)[1] for word in words]
            score = sum(scores) / len(scores)
            if min(scores) >= WORD_CUTOFF and score > best_score:
                best, best_score = candidate, score
    return best

def closest_name(name: str, choices: list[str]) -> str | None:
    """The choice closest to name, the earliest on a tie; for short names like teams and players."""
    match = process.extractOne(name, choices, scorer=fuzz.token_set_ratio, processor=normalize,
                               score_cutoff=NAME_CUTOFF)
    return match[0] if match else None
