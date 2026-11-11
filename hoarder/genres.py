"""Genre tags from the pack and folder. The filename is not used.

Stored like sample tags: " techno house ". A file can carry more than one.
"""

from __future__ import annotations

import re

GENRE_ORDER: tuple[str, ...] = (
    "techno",
    "trance",
    "house",
    "dnb",
    "dubstep",
    "phonk",
    "eurodance",
    "hardstyle",
    "garage",
    "trap",
    "pop",
    "hiphop",
    "ambient",
    "disco",
)

GENRE_LABELS: dict[str, str] = {
    "techno": "Techno",
    "trance": "Trance",
    "house": "House",
    "dnb": "Drum & Bass",
    "dubstep": "Dubstep",
    "phonk": "Phonk",
    "eurodance": "Eurodance",
    "hardstyle": "Hardstyle",
    "garage": "Garage",
    "trap": "Trap",
    "pop": "Pop",
    "hiphop": "Hip hop",
    "ambient": "Ambient",
    "disco": "Disco",
}

GENRE_SET = frozenset(GENRE_ORDER)

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("techno", re.compile(r"\btechno\b")),
    ("trance", re.compile(r"\btrance\b")),
    ("house", re.compile(r"\bhouse\b")),
    ("dnb", re.compile(r"\bdnb\b|\bdrum\s+and\s+bass\b|\bdrum\s+bass\b")),
    ("dubstep", re.compile(r"\bdubstep\b|\bdub\s+step\b")),
    ("phonk", re.compile(r"\bphonk\b")),
    ("eurodance", re.compile(r"\beurodance\b|\beuro\s+dance\b")),
    ("hardstyle", re.compile(r"\bhardstyle\b|\bhard\s+style\b")),
    ("garage", re.compile(r"\bgarage\b")),
    ("trap", re.compile(r"\btrap\b")),
    ("pop", re.compile(r"\bpop\b")),
    ("hiphop", re.compile(r"\bhip\s*hop\b|\bhiphop\b")),
    ("ambient", re.compile(r"\bambient\b")),
    ("disco", re.compile(r"\bdisco\b")),
)

_CAMEL = re.compile(r"([a-z])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"\s+")


def haystack(pack: str, folder: str) -> str:
    text = f"{pack} {folder}"
    text = _CAMEL.sub(r"\1 \2", text).lower()
    text = _NON_ALNUM.sub(" ", text)
    text = text.replace("psytrance", "trance")
    return f" {_SPACES.sub(' ', text).strip()} "


def classify(pack: str, folder: str) -> str:
    text = haystack(pack, folder)
    found = [genre for genre, pattern in _RULES if pattern.search(text)]
    return encode(found)


def encode(genres: list[str] | tuple[str, ...]) -> str:
    chosen = set(genres)
    ordered = [genre for genre in GENRE_ORDER if genre in chosen]
    if not ordered:
        return ""
    return " " + " ".join(ordered) + " "


def decode(stored: str | None) -> list[str]:
    found = set((stored or "").split())
    return [genre for genre in GENRE_ORDER if genre in found]


def known_genres(raw: str) -> list[str]:
    chosen: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        genre = part.strip().lower()
        if genre in GENRE_SET and genre not in seen:
            seen.add(genre)
            chosen.append(genre)
    return chosen
