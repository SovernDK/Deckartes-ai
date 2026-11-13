"""Matching names of shows, teams and players that differ in case, punctuation, accents or script.

Both sides of a comparison go through normalize, so "Pokémon" matches "pokemon" and a name in
Cyrillic or Japanese is transliterated rather than dropped.
"""

import re

from anyascii import anyascii


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", anyascii(text).lower()).strip()
