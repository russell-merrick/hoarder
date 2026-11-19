"""Musical key from a sample filename. A bare lowercase letter is not a key."""

from __future__ import annotations

import re

from hoarder.roles import is_drum

# A single lowercase "m" is minor. The words allow either case. Uppercase "M"
# alone is not minor, so a pack code like CM is left alone.
_KEY = re.compile(r"^([A-Ga-g])(#|b)?(?:(m)|((?i:min|minor|maj|major)))?$")
_NOTE = re.compile(r"^[A-Ga-g](#|b)?$")
_MODE = re.compile(r"(?i)^(min|minor|maj|major)$")
_SPLIT = re.compile(r"[^A-Za-z0-9#]+")

PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
_SHARPS = {sharp: flat for flat, sharp in _FLATS.items()}


def parse_key(name: str, folder: str = "") -> str:
    # A lone capital on a drum is a take letter (Kick_Punchy_A), not a key.
    bare_ok = not is_drum(name, folder)
    found = _last_key(name, bare_ok)
    if found:
        return found
    return _last_key(folder, bare_ok)


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


def is_minor(key: str) -> bool:
    return (key or "").endswith("m")


def spellings(key: str) -> list[str]:
    """Every way the catalog may store one key. A#m is also written Bbm."""
    match = re.fullmatch(r"([A-G])(#|b)?(m?)", key or "")
    if not match:
        return []
    note = match.group(1) + (match.group(2) or "")
    note = _FLATS.get(note, note)
    names = [note]
    if note in _SHARPS:
        names.append(_SHARPS[note])
    return [name + match.group(3) for name in names]


def semitone_shift(source: str, target: str) -> int | None:
    """Shortest repitch, from -6 to +6, that puts source in target's key.

    Relative keys share their notes, so an A minor loop in a C major song does
    not move. A key with no mode counts as major.
    """
    start = root_index(source)
    end = root_index(target)
    if start is None or end is None:
        return None
    if is_minor(source) and not is_minor(target):
        start += 3
    elif is_minor(target) and not is_minor(source):
        start -= 3
    delta = (end - start) % 12
    if delta > 6:
        delta -= 12
    return delta


def _last_key(text: str, bare_ok: bool) -> str:
    found = ""
    tokens = [token for token in _SPLIT.split(text) if token]
    for idx, token in enumerate(tokens):
        following = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        # "F Min" and "G Minor" write the mode as its own word.
        if _NOTE.match(token) and _MODE.match(following):
            token += following
        parsed = _parse_token(token, bare_ok)
        if parsed:
            found = parsed
    return found


def _parse_token(token: str, bare_ok: bool) -> str:
    match = _KEY.fullmatch(token)
    if not match:
        return ""
    note, accidental, minor_m, word = match.group(1), match.group(2) or "", match.group(3), match.group(4)
    bare = not accidental and not minor_m and not word
    if bare and (token[0].islower() or not bare_ok):
        return ""
    suffix = "m" if minor_m or (word and word.lower() in {"min", "minor"}) else ""
    return f"{note.upper()}{accidental}{suffix}"
