"""Musical key from a sample filename. A bare lowercase letter is not a key."""

from __future__ import annotations

import re

# A single lowercase "m" is minor. The words allow either case. Uppercase "M"
# alone is not minor, so a pack code like CM is left alone.
_KEY = re.compile(r"^([A-Ga-g])(#|b)?(?:(m)|((?i:min|minor|maj|major)))?$")
_SPLIT = re.compile(r"[^A-Za-z0-9#]+")

PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def parse_key(name: str, folder: str = "") -> str:
    found = _last_key(name)
    if found:
        return found
    return _last_key(folder)


def root_index(key: str) -> int | None:
    match = re.match(r"^([A-G])(#|b)?", key or "")
    if not match:
        return None
    name = match.group(1) + (match.group(2) or "")
    name = _FLATS.get(name, name)
    try:
        return PITCHES.index(name)
    except ValueError:
        return None


def semitone_shift(source: str, target: str) -> int | None:
    """Shortest repitch, from -6 to +6, that lands source's root on target."""
    start = root_index(source)
    end = root_index(target)
    if start is None or end is None:
        return None
    delta = (end - start) % 12
    if delta > 6:
        delta -= 12
    return delta


def _last_key(text: str) -> str:
    found = ""
    for token in _SPLIT.split(text):
        parsed = _parse_token(token)
        if parsed:
            found = parsed
    return found


def _parse_token(token: str) -> str:
    match = _KEY.fullmatch(token)
    if not match:
        return ""
    note, accidental, minor_m, word = match.group(1), match.group(2) or "", match.group(3), match.group(4)
    if not accidental and not minor_m and not word and token[0].islower():
        return ""
    suffix = "m" if minor_m or (word and word.lower() in {"min", "minor"}) else ""
    return f"{note.upper()}{accidental}{suffix}"
