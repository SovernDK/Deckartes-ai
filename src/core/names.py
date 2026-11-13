"""Matching names of shows, teams and players that differ in case or punctuation."""

import re


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
